"""Process-tree termination that actually reaches grandchildren.

``proc.kill()`` on Windows terminates only the direct child; a grandchild
(e.g. ``bash -> find``) survives and keeps the stdout pipe open, which is
exactly how a stuck worker command froze a whole delegation for 20+ minutes.
``kill_tree`` uses ``taskkill /T`` on Windows and, on POSIX, walks the whole
descendant tree and kills every process and its group, so every descendant
dies and pipes actually close.
"""

from __future__ import annotations

import os
import signal
import subprocess

def _descendants(pid: int) -> list[int]:
    """Every descendant of ``pid``, collected from a single ``ps`` snapshot.

    Collected BEFORE anything is killed: once a parent dies its children are
    reparented to launchd/init and the link back to ``pid`` is gone.
    """
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=10,
        ).stdout
    except Exception:  # noqa: BLE001 - fall back to the process group alone
        return []
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found: list[int] = []
    stack = [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            found.append(child)
            stack.append(child)
    return found


def _kill_posix_tree(pid: int) -> None:
    """Kill ``pid``, every descendant, and each of their process groups.

    Process groups alone are not enough: ``uv run`` starts its child in a
    process group of its own, so ``killpg`` on the shell's group left
    ``pytest`` running. In one live run that left two dozen processes scanning
    the whole disk for up to 1h47m after their workers were dead. Walking the
    tree reaches them whatever group they moved to.
    """
    own_group = os.getpgrp()
    for target in [pid, *_descendants(pid)]:
        try:
            group = os.getpgid(target)
            # Never signal our own group — that would kill the caller itself.
            if group != own_group:
                os.killpg(group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.kill(target, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def kill_tree(pid: int) -> bool:
    """Best-effort kill of ``pid`` and all of its descendants."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, stdin=subprocess.DEVNULL, timeout=15,
            )
        else:
            _kill_posix_tree(pid)
        return True
    except Exception:  # noqa: BLE001 - cleanup must never raise into the caller
        return False
