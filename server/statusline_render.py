"""Renders the active delegation into a pre-baked status-line file.

Design: the MCP server (already resident) does the rendering in Python and
writes a ready-to-print file at ``~/.monkey-army/statusline``. The status-line
script Claude Code runs is then a trivial, dependency-free reader (no python,
no jq, no JSON parsing on the shell side). Both ends are token-free — the
harness runs the reader locally and the server was already running.

File format (so the reader stays a 4-line bash script):

    <expiry_epoch>\n
    <one rendered line, may contain ANSI colors>

The reader prints line 2+ only while ``now <= expiry_epoch``. Expiry does the
lifecycle work: a running task refreshes it on every event; a finished task
writes a short-lived final line that then fades on its own; a blocked task
(needs_input) gets a long window so the question stays visible until answered.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from config import home_dir
from persistence import ACTIVE as _ACTIVE_STATUSES, TERMINAL as _TERMINAL_STATUSES

# ANSI status colors.
_RESET = "\033[0m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_GREY = "\033[90m"

# Per-status: (emoji, color, expiry-seconds-from-now).
_STYLE: dict[str, tuple[str, str, int]] = {
    "running": ("⏳", _CYAN, 150),      # refreshed on every event while active
    "verifying": ("🔍", _CYAN, 150),    # post-run server-side gate (§7.3) — still ACTIVE
    "needs_input": ("⚠", _YELLOW, 3600),  # a human may take a while — keep visible
    "succeeded": ("✓", _GREEN, 30),
    "integrated": ("✓", _GREEN, 30),
    "failed": ("✗", _RED, 30),
    "failed_scope": ("✗", _RED, 30),
    "failed_oversized": ("✗", _RED, 30),
    "failed_verification": ("✗", _RED, 30),
    "timeout": ("✗", _RED, 30),
    "cancelled": ("⊘", _GREY, 20),
}

_TERMINAL_LABEL: dict[str, str] = {
    "succeeded": "done",
    "integrated": "integrated",
    "failed": "failed",
    "failed_scope": "failed (out of scope)",
    "failed_oversized": "failed (diff too large)",
    "failed_verification": "failed (verification)",
    "timeout": "timed out",
    "cancelled": "cancelled",
}


def global_path() -> Path:
    return home_dir() / "statusline"


def short_id(task_id: str) -> str:
    """`mk_mrhufdhb_yqsldx` -> `mk_…yqsldx` (stable, glanceable)."""
    if len(task_id) <= 10:
        return task_id
    prefix = task_id.split("_", 1)[0]
    return f"{prefix}_…" + task_id[-6:]


def pretty_model(model: str | None) -> str | None:
    """`openai/combo/deepseek-main` -> `combo/deepseek-main`."""
    if not model:
        return None
    m = model.split(":", 1)[-1]        # drop `litellm:` router prefix
    return m.split("/", 1)[-1] or m    # drop `provider/` prefix


def _trim(text: str, limit: int = 44) -> str:
    text = " ".join(str(text).split())  # collapse whitespace/newlines
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render(job: dict[str, Any], now: float | None = None) -> tuple[int, str] | None:
    """Return ``(expiry_epoch, line)`` for ``job``, or None if nothing to show."""
    now = time.time() if now is None else now
    status = job.get("status")
    style = _STYLE.get(status)
    if style is None:
        return None
    emoji, color, ttl = style

    head = f"{emoji} monkey {short_id(job.get('taskId', '?'))}"
    parts: list[str] = []
    model = pretty_model(job.get("model"))
    if model:
        parts.append(model)

    if status in ("running", "verifying"):
        step = job.get("lastStep")
        if step:
            parts.append(f"step {step}")
        if job.get("progress"):
            parts.append(_trim(job["progress"]))
    elif status == "needs_input":
        q = job.get("question") or {}
        parts.append("asks: " + _trim(q.get("message", "input needed"), 40))
        parts.append("→ answer_worker")
    else:  # terminal
        parts.append(_TERMINAL_LABEL[status])
        if status in ("succeeded", "integrated"):
            n = len(job.get("filesChanged", []))
            if n:
                parts.append(f"{n} file{'s' if n != 1 else ''}")
        elif job.get("salvaged"):
            parts.append("work salvaged")
        if job.get("costUsd") is not None:
            parts.append(f"${job['costUsd']:.2f}")
        if status not in ("succeeded", "integrated") and job.get("error"):
            parts.append(_trim(job["error"], 40))

    line = head + " · " + " · ".join(parts) if parts else head
    return int(now + ttl), f"{color}{line}{_RESET}"


def _aggregate(snapshot: list[dict[str, Any]], now: float) -> tuple[int, str]:
    running = sum(1 for j in snapshot if j.get("status") in ("running", "verifying"))
    asks = sum(1 for j in snapshot if j.get("status") == "needs_input")
    done = sum(1 for j in snapshot if j.get("status") in _TERMINAL_STATUSES)
    cost = sum(j.get("costUsd") or 0 for j in snapshot)
    line = f"⏳ {running} running · ⚠ {asks} asks · ✓ {done} done · ${cost:.2f}"
    return int(now + 150), f"{_CYAN}{line}{_RESET}"


def write_statusline(job: dict[str, Any]) -> None:
    """Best-effort write of the global status-line file. Never raises.

    Normally renders `job`'s own line. When more than one job is ACTIVE in
    THIS process (jobs.all_jobs() — an in-memory, per-process view; a
    restarted server starts this count over) an aggregate line replaces it,
    so concurrent delegations don't fight over the single status-line slot.
    """
    try:
        from jobs import all_jobs
        snapshot = all_jobs()
    except Exception:  # noqa: BLE001 - status line must never break the caller
        snapshot = []
    active = sum(1 for j in snapshot if j.get("status") in _ACTIVE_STATUSES)
    rendered = _aggregate(snapshot, time.time()) if active > 1 else render(job)
    path = global_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if rendered is None:
            path.unlink(missing_ok=True)
            return
        until, line = rendered
        tmp = path.with_suffix(".tmp")
        tmp.write_text(f"{until}\n{line}\n", encoding="utf-8")
        tmp.replace(path)  # atomic: the reader never sees a half-written file
    except OSError:
        pass
