"""Job/repo persistence: on-disk job records, the repos index, the §5.5
status enum, and worker-stdout line parsers.

All per-repo state lives under ``<home>/repos/<slug>/`` — never inside the
user's own repository (I2 / §5.1). ``repos.json`` (slug -> absolute repo
path) is what lets a restarted server find jobs again: the in-memory job
registry does not survive a restart, but ``repos.json`` does, so
``jobs.get_job_with_fallback`` can scan every repo the server has ever seen.

The repos-index I/O (``slug_for``, ``repo_state_dir``, ``remember_repo``,
``all_repos``) lives HERE rather than in store.py so that jobs.py — which
already imports this module for job save/load — never needs to import
store.py, and store.py imports this module instead of the reverse. store.py
re-exports the same four names as part of its own public surface (§7.1) so
either module works as a caller's entry point.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from config import home_dir

RESULT_MARKER = "RESULT_JSON:"
PROGRESS_MARKER = "PROGRESS:"
QUESTION_MARKER = "QUESTION:"

# §5.5 status enum — single source of truth; every module that branches on
# job status imports these rather than hardcoding its own set.
ACTIVE = {"running", "needs_input", "verifying"}
TERMINAL = {
    "succeeded", "failed", "failed_verification", "failed_scope",
    "failed_oversized", "timeout", "cancelled", "integrated",
}
REVIEWABLE = TERMINAL - {"integrated"}
INTEGRABLE = {"succeeded"}


def find_last_result_line(stdout: str) -> str | None:
    """Return the last line starting with RESULT_JSON:, or None."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_MARKER):
            return line
    return None


def strip_result_marker(line: str) -> str:
    return line[len(RESULT_MARKER):] if line.startswith(RESULT_MARKER) else line


def parse_progress_line(line: str) -> dict[str, Any] | None:
    """Parsed payload of a PROGRESS: line, or None for non-PROGRESS/garbage.

    Never raises — malformed JSON and non-object payloads return None so the
    stdout consumer can ignore anything the worker (or a library it loads)
    happens to print.
    """
    if not line.startswith(PROGRESS_MARKER):
        return None
    try:
        obj = json.loads(line[len(PROGRESS_MARKER):])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def parse_question_line(line: str) -> dict[str, Any] | None:
    """Parsed payload of a QUESTION: line, or None.

    A valid question carries at least a string ``id`` and a string
    ``message``; anything else is treated as noise, mirroring
    parse_progress_line's never-raise contract.
    """
    if not line.startswith(QUESTION_MARKER):
        return None
    try:
        obj = json.loads(line[len(QUESTION_MARKER):])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    if not isinstance(obj.get("id"), str) or not obj["id"]:
        return None
    if not isinstance(obj.get("message"), str) or not obj["message"]:
        return None
    return obj


def progress_note(parsed: dict[str, Any]) -> str:
    """Human-readable note: explicit note > node#step > step N."""
    note = parsed.get("note")
    if isinstance(note, str) and note:
        return note
    node = parsed.get("node")
    step = parsed.get("step", "?")
    if isinstance(node, str) and node:
        return f"{node}#{step}"
    return f"step {step}"


# ── Repos index (§5.1, §7.1) ────────────────────────────────────────────

def repos_path() -> Path:
    return home_dir() / "repos.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def repos_index() -> dict[str, str]:
    return _read_json(repos_path())


def slug_for(repo_path: str | Path) -> str:
    """``f"{basename}-{sha1(abs_path).hexdigest()[:8]}"`` — stable per repo
    path, used to key both the repos index and its state directory."""
    abs_path = str(Path(repo_path).resolve())
    digest = hashlib.sha1(abs_path.encode("utf-8")).hexdigest()[:8]
    return f"{Path(abs_path).name}-{digest}"


def repo_state_dir(repo_path: str | Path) -> Path:
    """``<home>/repos/<slug>/`` — all state for this repo. Nothing is ever
    written inside the user's own repository (I2)."""
    return home_dir() / "repos" / slug_for(repo_path)


def remember_repo(repo_path: str | Path) -> str:
    """Record repo_path in repos.json keyed by its slug; return the slug.

    Called whenever a worktree is created so a server restart can still find
    the repo's jobs — the in-memory registry does not survive a restart,
    repos.json does.
    """
    abs_path = str(Path(repo_path).resolve())
    slug = slug_for(abs_path)
    index = repos_index()
    if index.get(slug) != abs_path:
        index[slug] = abs_path
        _write_json(repos_path(), index)
    return slug


def all_repos() -> list[str]:
    return list(repos_index().values())


# ── Job files ────────────────────────────────────────────────────────────

def job_file_path(repo: str, task_id: str) -> Path:
    return repo_state_dir(repo) / "jobs" / f"{task_id}.json"


def serialize_job(job: dict[str, Any]) -> str:
    # Runtime-only keys never belong in the file, even if a caller slipped
    # one into the dict.
    clean = {k: v for k, v in job.items() if k != "abort"}
    return json.dumps(clean, indent=2)


def deserialize_job(raw: str) -> dict[str, Any]:
    return json.loads(raw)


def save_job(job: dict[str, Any]) -> Path:
    path = job_file_path(job["repo"], job["taskId"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_job(job), encoding="utf-8")
    return path


def load_job(repo: str, task_id: str) -> dict[str, Any] | None:
    path = job_file_path(repo, task_id)
    try:
        return deserialize_job(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def delete_persisted_job(job: dict[str, Any]) -> None:
    path = job_file_path(job["repo"], job["taskId"])
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def find_persisted_job(task_id: str) -> dict[str, Any] | None:
    """Scan every repo the server has ever seen (repos.json) — not an
    in-memory set — so a job written before a restart stays reachable."""
    for repo in all_repos():
        job = load_job(repo, task_id)
        if job:
            return job
    return None
