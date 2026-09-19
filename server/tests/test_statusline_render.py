"""Status-line render tests — stdlib only, no mcp import."""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import statusline_render as sr


class TestRender(unittest.TestCase):
    def test_unknown_status_renders_nothing(self):
        self.assertIsNone(sr.render({"status": "queued", "taskId": "mk_1"}))
        self.assertIsNone(sr.render({"taskId": "mk_1"}))

    def test_running_line(self):
        now = 1_000_000
        until, line = sr.render(
            {"status": "running", "taskId": "mk_abc_yqsldx",
             "model": "openai/combo/deepseek-main", "lastStep": 24,
             "progress": "writing src/auth/tokens.js"},
            now=now,
        )
        self.assertEqual(until, now + 150)
        self.assertIn("mk_…yqsldx", line)
        self.assertIn("deepseek-main", line)
        self.assertIn("step 24", line)
        self.assertIn("tokens.js", line)

    def test_needs_input_has_long_ttl_and_hint(self):
        now = 1_000_000
        until, line = sr.render(
            {"status": "needs_input", "taskId": "mk_abc_yqsldx",
             "question": {"message": "which token TTL should I use?"}},
            now=now,
        )
        self.assertEqual(until, now + 3600)  # stays visible for the human
        self.assertIn("asks:", line)
        self.assertIn("answer_worker", line)

    def test_succeeded_shows_files_and_cost(self):
        _, line = sr.render(
            {"status": "succeeded", "taskId": "mk_abc_yqsldx",
             "filesChanged": ["a.js", "b.js", "c.js"], "costUsd": 0.2412},
        )
        self.assertIn("done", line)
        self.assertIn("3 files", line)
        self.assertIn("$0.24", line)

    def test_failed_shows_error_and_salvage(self):
        _, line = sr.render(
            {"status": "failed", "taskId": "mk_abc_yqsldx",
             "salvaged": True, "error": "rubric not satisfied: unsatisfied"},
        )
        self.assertIn("failed", line)
        self.assertIn("salvaged", line)
        self.assertIn("rubric", line)

    def test_progress_is_trimmed(self):
        _, line = sr.render(
            {"status": "running", "taskId": "mk_x",
             "progress": "x" * 200},
        )
        self.assertLess(len(line), 160)  # trimmed, not a wall of text

    def test_helpers(self):
        self.assertEqual(sr.short_id("mk_mrhufdhb_yqsldx"), "mk_…yqsldx")
        self.assertEqual(sr.short_id("mk_short"), "mk_short")
        self.assertEqual(sr.pretty_model("openai/combo/deepseek-main"), "combo/deepseek-main")
        self.assertEqual(sr.pretty_model("gpt-4o"), "gpt-4o")
        self.assertIsNone(sr.pretty_model(None))


class TestWriteStatusline(unittest.TestCase):
    def setUp(self):
        # Redirect the global path to a temp file for the duration of the test.
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = sr.global_path
        self._path = Path(self.tmp.name) / "statusline"
        sr.global_path = lambda: self._path

    def tearDown(self):
        sr.global_path = self._orig
        self.tmp.cleanup()

    def test_write_then_read_shape(self):
        sr.write_statusline({"status": "running", "taskId": "mk_x", "progress": "go"})
        lines = self._path.read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines[0].isdigit())
        self.assertGreaterEqual(int(lines[0]), int(time.time()))
        self.assertIn("monkey", lines[1])

    def test_write_unknown_status_removes_file(self):
        self._path.write_text("999\nstale\n", encoding="utf-8")
        sr.write_statusline({"status": "queued", "taskId": "mk_x"})
        self.assertFalse(self._path.exists())


if __name__ == "__main__":
    unittest.main()
