"""Server-written state that is NOT user configuration: supervisor notes per
repo, the repos index (re-exported from persistence.py — see that module's
docstring for why the repos-index I/O lives there), maintenance sweeps, and
`meta.json` (currently just `last_doctor_at`).

All user configuration lives in environment variables (config.py) — nothing
here is a place the user sets anything.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Any

from config import home_dir as home_dir  # re-exported: notes/prune callers use this
from persistence import TERMINAL
from persistence import (
    all_repos as all_repos,
    remember_repo as remember_repo,
    repo_state_dir as repo_state_dir,
    slug_for as slug_for,
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        # A corrupt file must not brick every tool; the next write repairs it.
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ── Notes (§7.1, §5.1) ───────────────────────────────────────────────────

_NOTES_HARD_CAP_CHARS = 4000  # file size cap; distinct from Defaults.notes_max_chars,
# which trims how much gets INJECTED into a worker brief.


def notes_path(repo_path: str | Path) -> Path:
    return repo_state_dir(repo_path) / "notes.md"


def read_notes(repo_path: str | Path, max_chars: int | None = None) -> str:
    try:
        text = notes_path(repo_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    return text if max_chars is None else text[-max_chars:]


def append_note(repo_path: str | Path, text: str) -> None:
    """Append a dated line; refuse if the file would grow past 4000 chars."""
    path = notes_path(repo_path)
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    line = f"- {date.today().isoformat()}: {text.strip()}\n"
    combined = existing + line
    if len(combined) > _NOTES_HARD_CAP_CHARS:
        raise ValueError(
            f"notes.md would exceed {_NOTES_HARD_CAP_CHARS} chars ({len(combined)}); "
            "prune old notes first"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(combined, encoding="utf-8")


# ── meta.json (server state, not user config) ────────────────────────────

def meta_path() -> Path:
    return home_dir() / "meta.json"


def record_doctor_run() -> float:
    """Persist meta.last_doctor_at — configure(action='status') surfaces it
    so the supervisor can see how stale the last health check is."""
    meta = _read_json(meta_path())
    now = time.time()
    meta["last_doctor_at"] = now
    _write_json(meta_path(), meta)
    return now


def last_doctor_at() -> float | None:
    return _read_json(meta_path()).get("last_doctor_at")


# ── Maintenance (§6.13 `configure(action='prune')`) ─────────────────────

def prune_repo(repo: str, older_than_days: int) -> dict[str, Any]:
    """Delete logs/patches/jobs of TERMINAL tasks older than N days, and
    `git worktree prune` the repo. Never raises — a corrupt job file is
    skipped, not fatal to the sweep."""
    cutoff = time.time() - older_than_days * 86400
    jobs_dir = repo_state_dir(repo) / "jobs"
    removed_jobs = removed_patches = removed_logs = 0
    for job_file in (jobs_dir.glob("*.json") if jobs_dir.is_dir() else []):
        try:
            job = json.loads(job_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("status") not in TERMINAL:
            continue
        ts = job.get("finishedAt") or job.get("startedAt") or 0
        if ts and ts > cutoff:
            continue
        task_id = job.get("taskId") or job_file.stem
        job_file.unlink(missing_ok=True)
        removed_jobs += 1
        patch = repo_state_dir(repo) / "patches" / f"{task_id}.diff"
        if patch.exists():
            patch.unlink()
            removed_patches += 1
        log = repo_state_dir(repo) / "logs" / f"{task_id}.jsonl"
        if log.exists():
            log.unlink()
            removed_logs += 1
    try:
        subprocess.run(["git", "worktree", "prune"], cwd=repo, capture_output=True, text=True,
                        stdin=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {"repo": repo, "jobs_removed": removed_jobs, "patches_removed": removed_patches, "logs_removed": removed_logs}
