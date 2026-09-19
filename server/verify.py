"""Preflight validation of the supervisor-provided test command.

Lesson from the field: a `test_command` whose *runner* is broken (not merely
failing assertions) makes the worker's acceptance gate unpassable — it then
chases phantom failures forever. Running the command once in the fresh
worktree and surfacing the outcome to the supervisor catches that before a
single worker token is spent.

Mirrors the worker's bash routing on Windows so the preflight sees the same
shell the worker will use.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Any

from proc_utils import kill_tree

OUTPUT_TAIL_CHARS = 800


def find_bash() -> str | None:
    """Locate bash on Windows (same strategy as worker/worker.py)."""
    found = shutil.which("bash")
    if found:
        return found
    for cand in (
        os.path.expandvars(r"%LOCALAPPDATA%\hermes\git\usr\bin\bash.exe"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ):
        if os.path.isfile(cand):
            return cand
    return None


def run_test_command(command: str, cwd: str, timeout_s: int = 120) -> dict[str, Any]:
    """Run ``command`` once in ``cwd``; report exit code + output tail.

    Never raises: any launch failure is folded into the report so
    run_dev_task can pass it along verbatim.
    """
    bash = find_bash() if os.name == "nt" else None
    script_path: str | None = None
    try:
        if bash:
            fd, script_path = tempfile.mkstemp(suffix=".sh", dir=cwd)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(command + "\n")
            popen_args: Any = [bash, script_path]
            shell = False
        else:
            popen_args = command
            shell = True
        proc = subprocess.Popen(
            popen_args, shell=shell, cwd=cwd,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
            start_new_session=(os.name != "nt"),
        )
        try:
            out, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid)
            try:
                out, _ = proc.communicate(timeout=10)
            except Exception:  # noqa: BLE001
                out = ""
            return {
                "ran": True, "exit_code": 124, "timed_out": True,
                "output_tail": (out or "")[-OUTPUT_TAIL_CHARS:],
            }
        return {
            "ran": True, "exit_code": proc.returncode, "timed_out": False,
            "output_tail": (out or "")[-OUTPUT_TAIL_CHARS:],
        }
    except Exception as e:  # noqa: BLE001
        return {"ran": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        if script_path:
            try:
                os.remove(script_path)
            except OSError:
                pass


def preflight_note(report: dict[str, Any]) -> str | None:
    """Supervisor-facing advice derived from a preflight report."""
    if not report.get("ran"):
        return "preflight could not run the test command; check it manually before trusting the worker's gate"
    if report.get("timed_out"):
        return (
            "test command did not finish within the preflight timeout — if it is expected to be slow, "
            "ignore this; otherwise fix it before delegating (a hanging gate stalls the worker)"
        )
    if report.get("exit_code") != 0:
        return (
            "test command exited non-zero. That is NORMAL if tests target code that does not exist yet — "
            "but read output_tail: if the RUNNER itself is broken (module/file not found on the test path, "
            "unknown option), fix test_command and re-delegate, because a broken acceptance gate makes the "
            "worker chase phantom failures"
        )
    return None
