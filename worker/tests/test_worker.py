# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "deepagents>=0.7.0a6",
#   "langchain-litellm",
#   "fastapi",
# ]
# ///
"""Unit tests for worker.py's pure/isolable helpers: build_model (fallback
chains), the dangerous-git command guard, the drive-scan guard, and the
proactive steering mailbox (check_steer_message / _append_steer_notice).

Not part of server/'s stdlib-only suite (worker.py needs the heavy deepagents
+ litellm stack) — run directly: `uv run worker/tests/test_worker.py`.
"""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import worker


class TestBuildModel(unittest.TestCase):
    def test_no_fallbacks_returns_bare_string_unchanged(self):
        self.assertEqual(
            worker.build_model("litellm:openai/combo-deepseek-main", None),
            "litellm:openai/combo-deepseek-main",
        )

    def test_empty_fallback_list_returns_bare_string_unchanged(self):
        self.assertEqual(
            worker.build_model("litellm:openai/combo-deepseek-main", []),
            "litellm:openai/combo-deepseek-main",
        )

    def test_fallbacks_build_a_chatlitellm_instance(self):
        model = worker.build_model(
            "litellm:openai/combo-deepseek-main",
            ["litellm:openai/combo-fallback", "litellm:anthropic/claude-haiku-4-5"],
        )
        from langchain_litellm import ChatLiteLLM

        self.assertIsInstance(model, ChatLiteLLM)
        self.assertEqual(model.model, "openai/combo-deepseek-main")
        self.assertEqual(
            model.model_kwargs["fallbacks"],
            ["openai/combo-fallback", "anthropic/claude-haiku-4-5"],
        )

    def test_bare_model_strips_provider_prefix_only_once(self):
        self.assertEqual(worker._bare_model("litellm:openai/combo-deepseek-main"), "openai/combo-deepseek-main")
        self.assertEqual(worker._bare_model("no-prefix-model"), "no-prefix-model")


class TestDangerousGitGuard(unittest.TestCase):
    def _blocked(self, cmd: str) -> bool:
        return bool(worker._DANGEROUS_GIT_RE.search(cmd))

    def test_blocks_push_merge_rebase(self):
        self.assertTrue(self._blocked("git push origin main"))
        self.assertTrue(self._blocked("git merge feature-branch"))
        self.assertTrue(self._blocked("git rebase -i HEAD~3"))
        self.assertTrue(self._blocked("GIT PUSH origin main"))  # case-insensitive

    def test_does_not_block_readonly_lookalikes(self):
        self.assertFalse(self._blocked("git merge-base HEAD main"))
        self.assertFalse(self._blocked("git log --merges"))
        self.assertFalse(self._blocked("git status"))
        self.assertFalse(self._blocked("echo merge"))

    def test_backend_execute_rejects_dangerous_git(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            backend = worker.SupervisedShellBackend(
                root_dir=tmp, virtual_mode=True, timeout=5, inherit_env=False, env={},
            )
            result = backend.execute("git push origin main")
            self.assertEqual(result.exit_code, 1)
            self.assertIn("blocked", result.output)

            ok = backend.execute("echo hi")
            self.assertEqual(ok.exit_code, 0)


class TestSteerMessage(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self._orig_comm_dir = os.environ.get("MONKEY_COMM_DIR")
        os.environ["MONKEY_COMM_DIR"] = self._tmp.name

    def tearDown(self):
        if self._orig_comm_dir is None:
            os.environ.pop("MONKEY_COMM_DIR", None)
        else:
            os.environ["MONKEY_COMM_DIR"] = self._orig_comm_dir
        self._tmp.cleanup()

    def _write_steer(self, message: str) -> None:
        path = os.path.join(self._tmp.name, "steer.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"message": message}, f)

    def test_no_pending_message_returns_none(self):
        self.assertIsNone(worker.check_steer_message())

    def test_reads_and_clears_pending_message(self):
        self._write_steer("use snake_case instead")
        self.assertEqual(worker.check_steer_message(), "use snake_case instead")
        # Cleared: a second read finds nothing left.
        self.assertIsNone(worker.check_steer_message())

    def test_malformed_file_does_not_raise(self):
        path = os.path.join(self._tmp.name, "steer.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not json")
        self.assertIsNone(worker.check_steer_message())

    def test_no_comm_dir_env_returns_none(self):
        os.environ.pop("MONKEY_COMM_DIR", None)
        self.assertIsNone(worker.check_steer_message())

    def test_append_steer_notice_passthrough_when_nothing_pending(self):
        self.assertEqual(worker._append_steer_notice("progress update delivered"), "progress update delivered")

    def test_append_steer_notice_appends_when_pending(self):
        self._write_steer("stop, use a different filename")
        text = worker._append_steer_notice("progress update delivered")
        self.assertIn("progress update delivered", text)
        self.assertIn("SUPERVISOR STEERING", text)
        self.assertIn("stop, use a different filename", text)

    def test_shell_backend_execute_surfaces_pending_steer(self):
        import tempfile

        self._write_steer("check the edge case for empty input")
        with tempfile.TemporaryDirectory() as tmp:
            backend = worker.SupervisedShellBackend(
                root_dir=tmp, virtual_mode=True, timeout=5, inherit_env=False, env={},
            )
            result = backend.execute("echo hi")
        self.assertIn("hi", result.output)
        self.assertIn("SUPERVISOR STEERING", result.output)
        self.assertIn("check the edge case for empty input", result.output)


if __name__ == "__main__":
    unittest.main()
