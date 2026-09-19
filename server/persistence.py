"""Job persistence and worker-stdout parsing. Mirror of src/persistence.ts.

The persisted JSON format is shared with the TypeScript server: camelCase
keys (taskId, costUsd, totalTokens, patchPath, filesChanged, ...), one file
per job at <repo>/<work_dir>/jobs/<taskId>.json. A job persisted by either
implementation must load in the other.

A "job" here is a plain dict carrying exactly the persisted fields. Runtime
handles (asyncio task, subprocess) are kept out of the dict — see jobs.py —
so serialization is trivially symmetric.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

RESULT_MARKER = "RESULT_JSON:"
PROGRESS_MARKER = "PROGRESS:"
QUESTION_MARKER = "QUESTION:"

DEFAULT_WORK_DIR = ".monkey-army"


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


def job_file_path(repo: str, task_id: str, work_dir: str = DEFAULT_WORK_DIR) -> Path:
    return Path(repo) / work_dir / "jobs" / f"{task_id}.json"


def serialize_job(job: dict[str, Any]) -> str:
    # Runtime-only keys never belong in the file, even if a caller slipped
    # one into the dict.
    clean = {k: v for k, v in job.items() if k != "abort"}
    return json.dumps(clean, indent=2)


def deserialize_job(raw: str) -> dict[str, Any]:
    return json.loads(raw)


def save_job(job: dict[str, Any], work_dir: str = DEFAULT_WORK_DIR) -> Path:
    path = job_file_path(job["repo"], job["taskId"], work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_job(job), encoding="utf-8")
    return path


def load_job(repo: str, task_id: str, work_dir: str = DEFAULT_WORK_DIR) -> dict[str, Any] | None:
    path = job_file_path(repo, task_id, work_dir)
    try:
        return deserialize_job(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def delete_persisted_job(job: dict[str, Any], work_dir: str = DEFAULT_WORK_DIR) -> None:
    path = job_file_path(job["repo"], job["taskId"], work_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


known_repos: set[str] = set()


def remember_repo(repo: str) -> None:
    if repo:
        known_repos.add(repo)


def find_persisted_job(task_id: str, work_dir: str = DEFAULT_WORK_DIR) -> dict[str, Any] | None:
    for repo in known_repos:
        job = load_job(repo, task_id, work_dir)
        if job:
            return job
    return None
