"""In-memory job registry, git worktree lifecycle, diff collection, cleanup.

Jobs are plain dicts (persisted shape — see persistence.py); runtime handles
live in the separate `runtime` map keyed by task id, so a restored-from-disk
job simply has no runtime entry and cannot be aborted.
"""

from __future__ import annotations

import asyncio
import secrets
import shutil
import string
import subprocess
import time
from pathlib import Path
from typing import Any

from config import home_dir
from persistence import (
    TERMINAL,
    delete_persisted_job,
    find_persisted_job,
    remember_repo,
    repo_state_dir,
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
    """Snapshot of every job live in this server process (liveness checks).

    Offloaded tools now call this from executor threads while the main
    thread's event loop mutates `_jobs` concurrently; `dict.values()` can
    raise "dictionary changed size during iteration" if a job is put/deleted
    mid-snapshot. One retry clears that without needing a lock for a
    read-mostly, best-effort snapshot.
    """
    try:
        return list(_jobs.values())
    except RuntimeError:
        return list(_jobs.values())


def put_job(job: dict[str, Any]) -> None:
    _jobs[job["taskId"]] = job


def delete_job(task_id: str) -> None:
    _jobs.pop(task_id, None)
    runtime.pop(task_id, None)


def get_job_with_fallback(task_id: str) -> dict[str, Any] | None:
    """Registry first, then a scan of every repo the server has ever seen
    (repos.json) — fixes the restart bug where a persisted job became
    unreachable once the in-memory registry was gone."""
    return _jobs.get(task_id) or find_persisted_job(task_id)


async def wait_for_tasks(
    task_ids: list[str], timeout_s: int | None, wait_timeout_s: int, hard_cap_s: int,
) -> dict[str, Any]:
    """§6.2: returns immediately if any listed task already needs input or is
    done; otherwise sleeps in 1s ticks until any task's status changes or the
    (hard-capped) timeout elapses. Lives here (stdlib) rather than in main.py
    so it's testable without the `mcp` dependency — main.py's wait_for_tasks
    tool is a thin wrapper that supplies the two Defaults-derived bounds.
    """
    budget = min(timeout_s or wait_timeout_s, hard_cap_s)
    start = time.time()

    def snapshot() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for tid in task_ids:
            j = get_job_with_fallback(tid)
            if not j:
                rows.append({"task_id": tid, "status": "unknown", "done": True, "error": "unknown task_id"})
                continue
            status = j.get("status")
            row: dict[str, Any] = {"task_id": tid, "status": status, "done": status in TERMINAL}
            if j.get("lastStep"):
                row["step"] = j["lastStep"]
            if j.get("costUsd") is not None:
                row["cost_usd"] = j["costUsd"]
            if status == "needs_input" and j.get("question"):
                row["question"] = {"id": j["question"].get("id"), "message": j["question"].get("message")}
            rows.append(row)
        return rows

    def fingerprint(rows: list[dict[str, Any]]) -> tuple:
        return tuple((r["task_id"], r["status"], (r.get("question") or {}).get("id")) for r in rows)

    initial = snapshot()
    if any(r["done"] or r["status"] == "needs_input" for r in initial):
        return {"elapsed_s": 0, "changed": False, "tasks": initial}

    initial_print = fingerprint(initial)
    while time.time() - start < budget:
        await asyncio.sleep(1)
        current = snapshot()
        if fingerprint(current) != initial_print:
            return {"elapsed_s": round(time.time() - start, 1), "changed": True, "tasks": current}

    return {"elapsed_s": round(time.time() - start, 1), "changed": False, "tasks": snapshot()}


def persist_job(job: dict[str, Any]) -> None:
    """Best-effort persistence; in-memory state stays authoritative."""
    try:
        save_job(job)
    except OSError:
        pass


def _git(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    # stdin detached: in the MCP server, inherited stdin is the protocol
    # channel and any child reading it corrupts the session.
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
        stdin=subprocess.DEVNULL,
    )


def create_worktree(repo_path: str, base_branch: str | None = None) -> dict[str, str]:
    """Create the disposable branch + worktree that isolates worker writes
    from the user's repo (I2). Lives under
    `<home>/repos/<slug>/worktrees/<task_id>` — never inside the repo.
    """
    repo = str(Path(repo_path).resolve())
    task_id = new_task_id()
    branch = f"monkey/{task_id}"
    slug = remember_repo(repo)
    wt_root = repo_state_dir(repo) / "worktrees"
    wt_root.mkdir(parents=True, exist_ok=True)
    worktree = str(wt_root / task_id)
    base = base_branch or _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    base_sha = _git(repo, "rev-parse", base).stdout.strip()
    _git(repo, "worktree", "add", "-b", branch, worktree, base)
    return {
        "taskId": task_id, "branch": branch, "worktree": worktree, "repo": repo,
        "slug": slug, "baseBranch": base, "baseSha": base_sha,
    }


def changed_files(worktree: str) -> list[str]:
    """Files changed (created/modified/deleted/renamed) in the worktree,
    uncommitted, working tree + index.

    Uses `git status --porcelain=v1 -z`: NUL-separated records survive
    filenames with spaces or newlines, and a rename/copy record is TWO
    NUL-terminated fields — `XY new-path\\0orig-path\\0` (the -z form drops
    the ` -> ` the non -z format uses, and puts the CURRENT path first) —
    which the non -z format cannot be parsed safely when a path contains a
    space. Never raises — [] if the worktree is gone or git errors.
    """
    try:
        out = _git(worktree, "status", "--porcelain=v1", "-z").stdout
    except (subprocess.CalledProcessError, OSError):
        return []
    tokens = out.split("\0")
    files: list[str] = []
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if not entry:
            continue
        status_code, path = entry[:2], entry[3:]
        files.append(path)
        if status_code[0] in ("R", "C"):
            # Rename/copy: the next NUL-terminated token is the ORIGIN path
            # (already superseded by `path` above) — consume and discard it.
            i += 1
    return files


def stage_files(worktree: str, files: list[str]) -> None:
    """Stage exactly `files`, one `git add -A -- <path>` per path — never a
    bare `git add -A`, which would sweep up changes a caller deliberately
    left unstaged (e.g. out-of-scope files, §7.3). `-A` per path still
    handles deletions.
    """
    for path in files:
        _git(worktree, "add", "-A", "--", path)


def diff_and_stat(worktree: str, base_sha: str) -> dict[str, Any]:
    """Staged diff against `base_sha`: patch text plus a numstat summary.

    Operates on whatever is currently staged, so a caller can stage only the
    in-scope subset (stage_files) and still get an accurate diffstat for
    exactly that subset. Binary files report 0/0 added/removed (numstat's
    `-`/`-`) but are still listed in `files`.

    `--no-renames` on both calls: rename detection collapses a rename to its
    destination path only (numstat's `old => new`), which would silently
    drop the ORIGIN path from `files`/`filesChanged` — a git_ops.integrate
    dirty_overlap check (or a scope_check) keyed only on the destination
    would then miss the user's own unrelated changes to the file under its
    old name. Plain add+delete pairs keep both paths visible.
    """
    patch = _git(worktree, "diff", "--cached", "--binary", "--no-renames", base_sha).stdout
    numstat = _git(worktree, "diff", "--cached", "--numstat", "--no-renames", base_sha).stdout
    files: list[dict[str, Any]] = []
    added = removed = 0
    for line in numstat.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added_str, removed_str, path = parts
        a = int(added_str) if added_str != "-" else 0
        r = int(removed_str) if removed_str != "-" else 0
        files.append({"path": path, "added": a, "removed": r})
        added += a
        removed += r
    return {"patch": patch, "files": files, "added": added, "removed": removed, "lines": added + removed}


def write_patch(slug: str, task_id: str, patch: str) -> Path:
    patch_dir = home_dir() / "repos" / slug / "patches"
    patch_dir.mkdir(parents=True, exist_ok=True)
    path = patch_dir / f"{task_id}.diff"
    path.write_text(patch, encoding="utf-8")
    return path


def commit_worktree(worktree: str, message: str) -> str | None:
    """Commit whatever is staged under a fixed monkey-army identity.

    Returns the new commit sha, or None when there was nothing to commit
    (not an error — a task whose only "change" was investigation, or a
    retry that restaged an already-committed state, legitimately commits
    nothing).
    """
    try:
        _git(
            worktree,
            "-c", "user.name=monkey-army", "-c", "user.email=monkey-army@localhost",
            "commit", "-m", message,
        )
    except subprocess.CalledProcessError as e:
        if "nothing to commit" in (e.stdout or "") + (e.stderr or ""):
            return None
        raise
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


def salvage_worktree(job: dict[str, Any]) -> bool:
    """Preserve a non-succeeded task's uncommitted work so nothing is lost.

    Field lesson: a worker can finish the actual work and then die
    (recursion overrun, timeout, cancel) before committing. This stages
    everything changed, writes the patch file (same shape as a success), and
    best-effort commits a WIP snapshot on the monkey branch. Returns True
    when there was anything to salvage. Never raises.
    """
    try:
        worktree = job["worktree"]
        files = changed_files(worktree)
        if not files:
            return False
        stage_files(worktree, files)
        patch = _git(worktree, "diff", "--cached", "--binary").stdout
    except (subprocess.CalledProcessError, OSError, KeyError):
        return False
    job["filesChanged"] = files
    job["patchPath"] = str(write_patch(job["slug"], job["taskId"], patch))
    try:
        commit_worktree(worktree, f"wip(monkey-army): salvage ({job.get('status', 'failed')})")
    except subprocess.CalledProcessError:
        pass  # staged + patch file already secure the work
    return True


def cleanup_job(job: dict[str, Any], delete_branch: bool = True) -> dict[str, Any]:
    """Remove worktree + branch + persisted file + comm dir. Caller checks
    the job is not running."""
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
        delete_persisted_job(job)
        result["persistedRemoved"] = True
    except OSError:
        pass

    shutil.rmtree(repo_state_dir(job["repo"]) / "comm" / job["taskId"], ignore_errors=True)

    delete_job(job["taskId"])
    return result
