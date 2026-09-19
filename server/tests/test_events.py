"""Event bus unit tests — stdlib only, no mcp import."""

import json
import os
import queue
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import events


class TestEventBus(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        os.environ["MONKEY_ARMY_HOME"] = self._home.name
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("MONKEY_ARMY_HOME", None)
        self._home.cleanup()

    def test_publish_writes_jsonl_and_stamps(self):
        ev = events.publish(self.repo, "t_1", {"kind": "shell", "note": "$ ls"})
        self.assertEqual(ev["task_id"], "t_1")
        self.assertIn("ts", ev)
        lines = events.log_path(self.repo, "t_1").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["note"], "$ ls")

    def test_log_path_lives_outside_the_repo(self):
        path = events.log_path(self.repo, "t_1")
        self.assertNotIn(str(Path(self.repo).resolve()), str(path))
        self.assertTrue(str(path).startswith(os.environ["MONKEY_ARMY_HOME"]))

    def test_subscriber_receives_then_unsubscribed_does_not(self):
        q = events.subscribe()
        try:
            events.publish(self.repo, "t_2", {"kind": "progress"})
            self.assertEqual(q.get_nowait()["task_id"], "t_2")
        finally:
            events.unsubscribe(q)
        events.publish(self.repo, "t_2", {"kind": "progress"})
        with self.assertRaises(queue.Empty):
            q.get_nowait()

    def test_full_queue_drops_instead_of_blocking(self):
        q = events.subscribe()
        try:
            for i in range(events.MAX_QUEUE + 10):
                events.publish(self.repo, "t_3", {"kind": "progress", "i": i})
            self.assertEqual(q.qsize(), events.MAX_QUEUE)
        finally:
            events.unsubscribe(q)

    def test_read_log_tolerates_garbage_and_limits(self):
        for i in range(5):
            events.publish(self.repo, "t_4", {"i": i})
        path = events.log_path(self.repo, "t_4")
        with path.open("a", encoding="utf-8") as f:
            f.write("not json\n[1,2,3]\n")
        out = events.read_log(self.repo, "t_4", limit=3)
        # garbage skipped, last valid events only
        self.assertEqual([e["i"] for e in out], [4])

    def test_read_log_missing_file(self):
        self.assertEqual(events.read_log(self.repo, "t_nope"), [])


class TestEventMessage(unittest.TestCase):
    def test_shell(self):
        self.assertEqual(events.event_message({"kind": "shell", "command": "npm test"}), "$ npm test")

    def test_question_and_blocker(self):
        self.assertIn("token TTL", events.event_message(
            {"kind": "question", "message": "which token TTL?"}))
        self.assertTrue(events.event_message({"kind": "blocker", "message": "x"}).startswith("❓"))

    def test_steer(self):
        msg = events.event_message({"kind": "steer", "message": "use snake_case instead"})
        self.assertIn("steering", msg)
        self.assertIn("snake_case", msg)

    def test_terminal_states(self):
        self.assertIn("✓ done", events.event_message(
            {"kind": "succeeded", "files_changed": 4, "cost_usd": 0.24}))
        self.assertIn("4 files", events.event_message(
            {"kind": "succeeded", "files_changed": 4}))
        self.assertTrue(events.event_message({"kind": "failed", "error": "boom"}).startswith("✗"))
        self.assertTrue(events.event_message({"kind": "cancelled"}).startswith("⊘"))

    def test_started_with_model(self):
        self.assertIn("deepseek-main", events.event_message(
            {"kind": "started", "model": "deepseek-main"}))

    def test_progress_fallback_and_truncation(self):
        self.assertEqual(events.event_message({"kind": "progress", "note": "hello"}), "hello")
        self.assertLessEqual(len(events.event_message({"kind": "progress", "note": "x" * 500})), 160)


if __name__ == "__main__":
    unittest.main()
