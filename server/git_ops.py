"""§7.4: merge an approved task's patch into the user's own branch.

I6: never leaves the tree half-merged. The dry run is a STRICT
`git apply --index --check` with NO `--3way` — verified.md item 5 found that
`--check --3way` together exit 0 even when the 3-way apply would actually
conflict ("Applied patch ... with conflicts", exit 0), which would let a
conflicted merge through undetected. Strict mode fails loudly instead
(`error: patch failed: f:1` / `error: f: patch does not apply`), so a
non-zero dry run always means nothing was touched.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from jobs import cleanup_job
from persistence import save_job

# git apply's conflict messages vary by cause: a context mismatch on an
# existing file ("patch failed" / "patch does not apply"), or — just as
# common here, since most tasks ADD new files — the target already existing
# ("already exists in index"/"in working directory") because an unrelated
# change created a same-named file after the task's base_sha was captured.
_CONFLICT_PATTERNS = [
    re.compile(r"error: patch failed: ([^:]+):\d+"),
    re.compile(r"error: ([^:]+): patch does not apply"),
    re.compile(r"error: ([^:]+): already exists in"),
]


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        stdin=subprocess.DEVNULL, check=check,
    )


def _has_in_progress_operation(repo: str) -> bool:
    try:
        git_dir = Path(_git(repo, "rev-parse", "--git-dir").stdout.strip())
    except subprocess.CalledProcessError:
        return False
    if not git_dir.is_absolute():
        git_dir = Path(repo) / git_dir
    return any((git_dir / marker).exists() for marker in
               ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "rebase-merge", "rebase-apply"))


def _current_branch(repo: str) -> str | None:
    """None means detached HEAD — always treated as a mismatch (§7.4)."""
    out = _git(repo, "symbolic-ref", "--short", "HEAD", check=False)
    return out.stdout.strip() if out.returncode == 0 else None


def _parse_conflict_files(stderr: str) -> list[str]:
    files: set[str] = set()
    for line in stderr.splitlines():
        for pattern in _CONFLICT_PATTERNS:
            m = pattern.search(line)
            if m:
                files.add(m.group(1))
                break
    return sorted(files)


_RE_DISPATCH_SUGGESTION = "re-dispatch this task against the current branch (base moved), then integrate"
_ALREADY_EXISTS_SUGGESTION = (
    "an untracked file at this path exists in your tree; move it or commit it, then integrate again"
)


def _conflict_suggestion(stderr: str) -> str:
    """A file that already exists in the working tree isn't a moved base —
    re-dispatching won't fix it — so it gets its own actionable suggestion
    instead of the generic "base moved" one."""
    if "already exists in working directory" in stderr:
        return _ALREADY_EXISTS_SUGGESTION
    return _RE_DISPATCH_SUGGESTION


def _patch_paths(repo: str, patch_path: str) -> list[str]:
    """Every path `patch_path` touches, via `git apply --numstat -z` — safe
    to call before the patch is applied (it only parses the patch text).
    Fields within a record stay tab-separated (added\\tremoved\\tpath); `-z`
    only changes the RECORD terminator from newline to NUL, so a path
    containing a newline still parses safely.
    """
    out = _git(repo, "apply", "--numstat", "-z", patch_path).stdout
    paths = []
    for record in out.split("\0"):
        parts = record.split("\t")
        if len(parts) == 3:
            paths.append(parts[2])
    return paths


def integrate(
    job: dict[str, Any], repo: str, message: str | None, mode: str,
    allow_branch_mismatch: bool,
) -> dict[str, Any]:
    """Algorithm (§7.4). Every precondition failure returns before anything
    is modified; the dry-run/apply pair guarantees the same for a conflict.
    """
    task_id = job["taskId"]

    if job.get("status") != "succeeded" or (job.get("review") or {}).get("verdict") != "approve":
        return {
            "task_id": task_id, "integrated": False, "reason": "not_approved",
            "suggestion": "call review_task(task_id, 'approve') first",
        }

    if _has_in_progress_operation(repo):
        return {
            "task_id": task_id, "integrated": False, "reason": "in_progress_operation",
            "suggestion": "finish or abort the repo's in-progress git operation (merge/rebase/cherry-pick) first",
        }

    current = _current_branch(repo)
    if current is None or (current != job.get("baseBranch") and not allow_branch_mismatch):
        return {
            "task_id": task_id, "integrated": False, "reason": "branch_mismatch",
            "details": {"current_branch": current, "expected": job.get("baseBranch")},
            "suggestion": "checkout the task's base branch, or pass allow_branch_mismatch=True if intentional",
        }

    # Fresh from the repo (not the worktree) so it covers every attempt's
    # commits, not just the last one. --no-renames: a rename patch's
    # "rename from/to" header would make --numstat below report only the
    # new path, silently dropping the old one from the commit pathspec.
    patch = _git(repo, "diff", "--binary", "--no-renames", job["baseSha"], job["branch"]).stdout
    # Outside the repo (I2 — nothing is ever written inside the user's tree),
    # even transiently.
    fd, patch_path = tempfile.mkstemp(suffix=".diff")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(patch)

        # The exact paths this patch touches, BEFORE applying anything — this
        # (not job["filesChanged"], which is the WORKTREE's view and can be
        # stale relative to what actually landed in the patch) drives both
        # the dirty-overlap check below and the pathspec-limited commit
        # further down, so both act on exactly what this patch changes.
        touched_paths = _patch_paths(repo, patch_path)

        if touched_paths:
            dirty = _git(repo, "status", "--porcelain", "--", *touched_paths).stdout
            if dirty.strip():
                return {
                    "task_id": task_id, "integrated": False, "reason": "dirty_overlap",
                    "details": {"files": [line[3:] for line in dirty.splitlines() if line.strip()]},
                    "suggestion": "commit or stash your own changes to these files before integrating",
                }

        check = _git(repo, "apply", "--index", "--check", patch_path, check=False)
        if check.returncode != 0:
            return {
                "task_id": task_id, "integrated": False, "reason": "conflict",
                "details": {"files": _parse_conflict_files(check.stderr), "stderr": check.stderr[-2000:]},
                "suggestion": _conflict_suggestion(check.stderr),
            }

        apply = _git(repo, "apply", "--index", patch_path, check=False)
        if apply.returncode != 0:
            # The check just passed — this would mean the tree moved between
            # the two calls. Report it the same way; nothing else touched it.
            return {
                "task_id": task_id, "integrated": False, "reason": "conflict",
                "details": {"files": _parse_conflict_files(apply.stderr), "stderr": apply.stderr[-2000:]},
                "suggestion": _conflict_suggestion(apply.stderr),
            }
    finally:
        Path(patch_path).unlink(missing_ok=True)

    result: dict[str, Any] = {"task_id": task_id, "integrated": True, "mode": mode, "files": touched_paths}

    if mode == "commit" and touched_paths:
        # The USER's own git identity — no `-c user.name=...` override, unlike
        # the disposable worktree commits, which land in the user's real history.
        # Pathspec-limited: `git commit -m <msg>` with NO pathspec commits the
        # WHOLE index, which would sweep in any unrelated changes the user
        # already had staged of their own.
        commit_message = message or (
            f"{job.get('title', task_id)}\n\n"
            f"monkey-army task {task_id} (attempts: {job.get('attempt', 1)}, worker: {job.get('model')})"
        )
        _git(repo, "commit", "-m", commit_message, "--", *touched_paths)
        commit_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        job["integratedSha"] = commit_sha
        result["commit_sha"] = commit_sha

    job["integrateMode"] = mode
    job["status"] = "integrated"
    job["finishedAt"] = time.time()
    cleaned = cleanup_job(job, delete_branch=True)
    _git(repo, "worktree", "prune", check=False)

    # cleanup_job deletes jobs/<id>.json — persist a minimal record back so
    # batch status / task_result can still read the outcome; `prune` removes
    # it later (§7.4 step 7). Keeps everything a batch report (§5.4) totals
    # up (costUsd, totalTokens, priced, diffstat added/removed) plus enough
    # to explain the outcome (model, filesChanged, verification.passed,
    # review) — just drops the bulky per-file diffstat list and the patch.
    diffstat = job.get("diffstat") or {}
    verification = job.get("verification") or {}
    save_job({
        "taskId": task_id, "repo": repo, "slug": job["slug"], "status": "integrated",
        "finishedAt": job.get("finishedAt"),
        "title": job.get("title"), "integratedSha": job.get("integratedSha"),
        "integrateMode": mode, "attempt": job.get("attempt", 1),
        "batchId": job.get("batchId"), "batchKey": job.get("batchKey"),
        "model": job.get("model"), "costUsd": job.get("costUsd"), "totalTokens": job.get("totalTokens"),
        "priced": job.get("priced", False), "modelsSeen": job.get("modelsSeen", []),
        "filesChanged": job.get("filesChanged", []),
        "diffstat": {"added": diffstat.get("added"), "removed": diffstat.get("removed"), "lines": diffstat.get("lines")},
        "verification": {"passed": verification.get("passed")},
        "review": job.get("review"),
    })

    result["cleaned"] = {"worktreeRemoved": cleaned["worktreeRemoved"], "branchDeleted": cleaned["branchDeleted"]}
    return result


def assert_end_state(repo: str, task_ids: list[str]) -> dict[str, Any]:
    """Zero worktrees/branches left for `task_ids` — the end-of-batch
    guarantee (§1: exactly one worktree and the branch the user started
    on, regardless of how many were created during execution)."""
    wt_out = _git(repo, "worktree", "list", "--porcelain", check=False).stdout
    worktree_paths = [line[len("worktree "):] for line in wt_out.splitlines() if line.startswith("worktree ")]
    leftover_worktrees = [p for p in worktree_paths[1:] if any(tid in p for tid in task_ids)]

    branch_out = _git(repo, "branch", "--list", "monkey/*", check=False).stdout
    branches = [line.strip().lstrip("* ").strip() for line in branch_out.splitlines() if line.strip()]
    leftover_branches = [b for b in branches if any(tid in b for tid in task_ids)]

    return {
        "worktreesLeft": len(leftover_worktrees), "branchesLeft": len(leftover_branches),
        "details": {"worktrees": leftover_worktrees, "branches": leftover_branches},
    }
