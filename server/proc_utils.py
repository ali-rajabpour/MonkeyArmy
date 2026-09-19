"""Process-tree termination that actually reaches grandchildren.

``proc.kill()`` on Windows terminates only the direct child; a grandchild
(e.g. ``bash -> find``) survives and keeps the stdout pipe open, which is
exactly how a stuck worker command froze a whole delegation for 20+ minutes.
``kill_tree`` uses ``taskkill /T`` on Windows and process groups on POSIX so
every descendant dies and pipes actually close.
"""

from __future__ import annotations

import os
import signal
import subprocess


def kill_tree(pid: int) -> bool:
    """Best-effort kill of ``pid`` and all of its descendants."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, stdin=subprocess.DEVNULL, timeout=15,
            )
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        return True
    except Exception:  # noqa: BLE001 - cleanup must never raise into the caller
        return False
