"""In-memory job registry, git worktree lifecycle, diff collection, cleanup.

Mirror of src/jobs.ts. Jobs are plain dicts (persisted shape — see
persistence.py); runtime handles live in the separate `runtime` map keyed by
task id, so a restored-from-disk job simply has no runtime entry and cannot
be aborted — same semantics as the TypeScript server.
"""

from __future__ import annotations

import secrets
import string
import subprocess
import time
from pathlib import Path
from typing import Any

from persistence import (
    delete_persisted_job,
    find_persisted_job,
    remember_repo,
    save_job,
)

_jobs: dict[str, dict[str, Any]] = {}
runtime: dict[str, dict[str, Any]] = {}

_BASE36 = string.digits + string.ascii_lowercase


def _to_base36(n: int) -> str:
    if n == 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(_BASE36[r])
    return "".join(reversed(out))


def new_task_id() -> str:
    ts = _to_base36(int(time.time() * 1000))
    rand = "".join(secrets.choice(_BASE36) for _ in range(6))
    return f"mk_{ts}_{rand}"


def get_job(task_id: str) -> dict[str, Any] | None:
    return _jobs.get(task_id)


def all_jobs() -> list[dict[str, Any]]:
    """Snapshot of every job live in this server process (liveness checks)."""
    return list(_jobs.values())


def put_job(job: dict[str, Any]) -> None:
    _jobs[job["taskId"]] = job


def delete_job(task_id: str) -> None:
    _jobs.pop(task_id, None)
    runtime.pop(task_id, None)


def get_job_with_fallback(task_id: str, work_dir: str) -> dict[str, Any] | None:
    """Registry first, then the persisted JSON (jobs from a previous process)."""
    return _jobs.get(task_id) or find_persisted_job(task_id, work_dir)


def persist_job(job: dict[str, Any], work_dir: str) -> None:
    """Best-effort persistence; in-memory state stays authoritative."""
    try:
        save_job(job, work_dir)
    except OSError:
        pass


def _git(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    # stdin detached: in the MCP server, inherited stdin is the protocol
    # channel and any child reading it corrupts the session.
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
        stdin=subprocess.DEVNULL,
    )


def create_worktree(work_dir: str, repo_path: str, base_branch: str | None = None) -> dict[str, str]:
    """Create the disposable branch + worktree that isolates worker writes."""
    repo = str(Path(repo_path).resolve())
    task_id = new_task_id()
    branch = f"monkey/{task_id}"
    wt_root = Path(repo) / work_dir / "worktrees"
    wt_root.mkdir(parents=True, exist_ok=True)
    worktree = str(wt_root / task_id)
    base = base_branch or _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    _git(repo, "worktree", "add", "-b", branch, worktree, base)
    remember_repo(repo)
    return {"taskId": task_id, "branch": branch, "worktree": worktree, "repo": repo}


def worktree_changed_files(worktree: str) -> list[str]:
    """Files the worker has created/modified in its worktree so far (uncommitted).

    A live audit for get_task_progress: the worker commits only at salvage/success,
    so mid-run its work shows up as porcelain status. Never raises — returns [] if
    the worktree is gone or git errors.
    """
    try:
        out = _git(worktree, "status", "--porcelain").stdout
    except (subprocess.CalledProcessError, OSError):
        return []
    files: list[str] = []
    for line in out.splitlines():
        name = line[3:].strip() if len(line) > 3 else line.strip()
        if name:
            # `R  old -> new` rename form: keep the destination path.
            files.append(name.split(" -> ")[-1])
    return files


def collect_diff(work_dir: str, repo: str, worktree: str, task_id: str) -> dict[str, Any]:
    """Produce the git patch + list of files the worker changed."""
    _git(worktree, "add", "-A")
    diff = _git(worktree, "diff", "--cached").stdout
    names = _git(worktree, "diff", "--cached", "--name-only").stdout
    patch_dir = Path(repo) / work_dir / "patches"
    patch_dir.mkdir(parents=True, exist_ok=True)
    patch_path = patch_dir / f"{task_id}.diff"
    patch_path.write_text(diff, encoding="utf-8")
    files_changed = [line.strip() for line in names.splitlines() if line.strip()]
    return {"patchPath": str(patch_path), "filesChanged": files_changed}


def salvage_worktree(work_dir: str, job: dict[str, Any]) -> bool:
    """Preserve a non-succeeded task's uncommitted work so nothing is lost.

    Field lesson: a worker can finish the actual work and then die (recursion
    overrun, timeout, cancel) before committing — leaving fetch_task_result
    empty and the salvage entirely manual. This stages everything, writes the
    patch file (same shape as a success), and best-effort commits a WIP
    snapshot on the monkey branch. Returns True when there was anything to
    salvage. Never raises.
    """
    try:
        diff = collect_diff(work_dir, job["repo"], job["worktree"], job["taskId"])
    except (subprocess.CalledProcessError, OSError, KeyError):
        return False
    if not diff.get("filesChanged"):
        return False
    job.update(diff)
    try:
        _git(
            job["worktree"],
            "-c", "user.name=monkey-army",
            "-c", "user.email=monkey-army@localhost",
            "commit", "-m",
            f"wip(monkey-army): salvage snapshot ({job.get('status', 'failed')})",
        )
    except subprocess.CalledProcessError:
        pass  # staged + patch file already secure the work
    return True


def cleanup_job(work_dir: str, job: dict[str, Any], delete_branch: bool = True) -> dict[str, Any]:
    """Remove worktree + branch + persisted file. Caller checks job is not running."""
    result = {
        "taskId": job["taskId"],
        "worktreeRemoved": False,
        "branchDeleted": False,
        "persistedRemoved": False,
    }
    try:
        _git(job["repo"], "worktree", "remove", "--force", job["worktree"])
        result["worktreeRemoved"] = True
    except subprocess.CalledProcessError:
        # Already gone or locked; branch + file cleanup still proceed.
        pass

    if delete_branch:
        try:
            _git(job["repo"], "branch", "-D", job["branch"])
            result["branchDeleted"] = True
        except subprocess.CalledProcessError:
            pass

    try:
        delete_persisted_job(job, work_dir)
        result["persistedRemoved"] = True
    except OSError:
        pass

    delete_job(job["taskId"])
    return result
