"""Preflight + process-tree kill tests — stdlib only, no mcp import."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verify import oversize_check, post_run_verify, preflight_note, run_command, scope_check
from proc_utils import kill_tree
from config import Defaults


class TestRunCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_passing_command(self):
        report = run_command("echo preflight-ok", self.tmp.name)
        self.assertTrue(report["ran"])
        self.assertEqual(report["exit_code"], 0)
        self.assertIn("preflight-ok", report["output_tail"])
        self.assertIsNone(preflight_note(report))

    def test_broken_runner_surfaces_nonzero_and_note(self):
        report = run_command("definitely-not-a-real-command-xyz", self.tmp.name)
        self.assertTrue(report["ran"])
        self.assertNotEqual(report["exit_code"], 0)
        note = preflight_note(report)
        self.assertIsNotNone(note)
        self.assertIn("NORMAL", note)

    def test_timeout_kills_and_reports(self):
        report = run_command("sleep 30", self.tmp.name, timeout_s=2)
        self.assertTrue(report["ran"])
        self.assertTrue(report["timed_out"])
        self.assertEqual(report["exit_code"], 124)
        self.assertIn("preflight timeout", preflight_note(report))


class TestScopeCheck(unittest.TestCase):
    def test_empty_allowed_is_unrestricted(self):
        result = scope_check(["a.py", "b.py"], None)
        self.assertTrue(result["ok"])
        self.assertTrue(result["unrestricted"])
        self.assertEqual(result["violations"], [])
        self.assertTrue(scope_check(["a.py"], [])["unrestricted"])

    def test_double_star_glob(self):
        result = scope_check(["src/a.py", "src/sub/b.py", "src/sub/deep/c.py", "other.py"], ["src/**/*.py"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["violations"], ["other.py"])

    def test_exact_file_match(self):
        result = scope_check(["README.md"], ["README.md"])
        self.assertTrue(result["ok"])

    def test_rename_destination_is_checked(self):
        # jobs.changed_files already resolves renames to the destination path
        # (§7.2) — scope_check only ever sees that, never the "old => new" form.
        result = scope_check(["src/new_name.py"], ["src/*.py"])
        self.assertTrue(result["ok"])

    def test_untracked_file_is_checked_like_any_other(self):
        result = scope_check(["scratch.txt"], ["src/**"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["violations"], ["scratch.txt"])


class TestOversizeCheck(unittest.TestCase):
    def test_under_cap_is_fine(self):
        self.assertFalse(oversize_check({"lines": 100}, 300))

    def test_over_cap_is_oversized(self):
        self.assertTrue(oversize_check({"lines": 301}, 300))

    def test_exactly_at_cap_is_fine(self):
        self.assertFalse(oversize_check({"lines": 300}, 300))


class TestPostRunVerify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Defaults(home=Path(self.tmp.name), verify_timeout_s=10)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_commands_set_passes_trivially(self):
        result = post_run_verify({"worktree": self.tmp.name}, self.cfg)
        self.assertTrue(result["passed"])
        self.assertEqual(result["steps"], [])

    def test_all_three_run_in_order_when_all_pass(self):
        job = {
            "worktree": self.tmp.name,
            "testCommand": "echo test-step",
            "verifyCommand": "echo verify-step",
            "lintCommand": "echo lint-step",
        }
        result = post_run_verify(job, self.cfg)
        self.assertTrue(result["passed"])
        self.assertEqual([s["name"] for s in result["steps"]], ["test", "verify", "lint"])

    def test_stops_at_first_failure(self):
        job = {"worktree": self.tmp.name, "testCommand": "exit 1", "verifyCommand": "echo should-not-run"}
        result = post_run_verify(job, self.cfg)
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(result["steps"][0]["name"], "test")


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
