"""Preflight + process-tree kill tests — stdlib only, no mcp import."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verify import preflight_note, run_test_command
from proc_utils import kill_tree


class TestRunTestCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_passing_command(self):
        report = run_test_command("echo preflight-ok", self.tmp.name)
        self.assertTrue(report["ran"])
        self.assertEqual(report["exit_code"], 0)
        self.assertIn("preflight-ok", report["output_tail"])
        self.assertIsNone(preflight_note(report))

    def test_broken_runner_surfaces_nonzero_and_note(self):
        report = run_test_command("definitely-not-a-real-command-xyz", self.tmp.name)
        self.assertTrue(report["ran"])
        self.assertNotEqual(report["exit_code"], 0)
        note = preflight_note(report)
        self.assertIsNotNone(note)
        self.assertIn("NORMAL", note)

    def test_timeout_kills_and_reports(self):
        report = run_test_command("sleep 30", self.tmp.name, timeout_s=2)
        self.assertTrue(report["ran"])
        self.assertTrue(report["timed_out"])
        self.assertEqual(report["exit_code"], 124)
        self.assertIn("preflight timeout", preflight_note(report))


class TestKillTree(unittest.TestCase):
    def test_kills_python_child(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            # New session/group, same as every real kill_tree caller — otherwise
            # os.killpg(pid's group) hits the test runner's own group too.
            start_new_session=(os.name != "nt"),
        )
        try:
            self.assertTrue(kill_tree(proc.pid))
            deadline = time.time() + 10
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.1)
            self.assertIsNotNone(proc.poll(), "process should be dead after kill_tree")
        finally:
            if proc.poll() is None:
                proc.kill()


if __name__ == "__main__":
    unittest.main()
