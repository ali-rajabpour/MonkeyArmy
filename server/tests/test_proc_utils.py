"""kill_tree must reach every descendant, whatever process group it moved to.

`uv run` starts its child in a process group of its own, so the old
killpg-only implementation killed the shell and left `pytest` running. In one
live run that left two dozen processes scanning the whole disk for up to
1h47m after their workers had died.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proc_utils import _descendants, kill_tree

# A child that re-parents itself into a NEW process group, then reports its pid.
_CHILD = (
    "import os, time; os.setpgid(0, 0); "
    "print(os.getpid(), flush=True); time.sleep(60)"
)
_PARENT = (
    "import subprocess, sys; "
    f"p = subprocess.Popen([sys.executable, '-c', {_CHILD!r}], stdout=subprocess.PIPE, text=True); "
    "print(p.stdout.readline().strip(), flush=True); p.wait()"
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@unittest.skipIf(os.name == "nt", "POSIX process groups")
class TestKillTree(unittest.TestCase):
    def _spawn(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", _PARENT],
            stdout=subprocess.PIPE, text=True, start_new_session=True,
        )
        grandchild = int(proc.stdout.readline().strip())
        return proc, grandchild

    def test_descendants_are_found_across_process_groups(self):
        proc, grandchild = self._spawn()
        try:
            self.assertNotEqual(os.getpgid(grandchild), os.getpgid(proc.pid))
            self.assertIn(grandchild, _descendants(proc.pid))
        finally:
            kill_tree(proc.pid)
            proc.wait(timeout=10)

    def test_grandchild_in_another_group_is_killed(self):
        proc, grandchild = self._spawn()
        self.assertTrue(kill_tree(proc.pid))
        proc.wait(timeout=10)
        deadline = time.time() + 5
        while _alive(grandchild) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(_alive(grandchild), "grandchild in another process group survived")

    def test_never_signals_its_own_process_group(self):
        # A child left in OUR group must be killed individually, never via
        # killpg — that would take down the caller (the MCP server) too.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.assertEqual(os.getpgid(child.pid), os.getpgrp())
        self.assertTrue(kill_tree(child.pid))
        child.wait(timeout=10)
        self.assertTrue(_alive(os.getpid()))  # we are still here


if __name__ == "__main__":
    unittest.main()
