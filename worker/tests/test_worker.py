# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "deepagents==0.7.15",
#   "langchain-litellm==0.7.2",
#   "litellm==1.101.0",
#   "fastapi==0.116.1",
# ]
# ///
"""Unit tests for worker.py's pure/isolable helpers: build_model,
git_command_allowed, the drive-scan guard, CostTracker, build_shell_env, and
the proactive steering mailbox (check_steer_message / _append_steer_notice).

Not part of server/'s stdlib-only suite (worker.py needs the heavy deepagents
+ litellm stack) — run directly: `uv run worker/tests/test_worker.py`.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import worker


class TestBuildModel(unittest.TestCase):
    def test_sets_model_api_base_api_key(self):
        model = worker.build_model("openai/combo/deepseek-main", "http://9router.local/v1", "sk-abc", [], {})
        from langchain_litellm import ChatLiteLLM

        self.assertIsInstance(model, ChatLiteLLM)
        # Full litellm string including the provider prefix — NOT stripped.
        self.assertEqual(model.model, "openai/combo/deepseek-main")
        self.assertEqual(model.api_base, "http://9router.local/v1")
        self.assertEqual(model.api_key, "sk-abc")

    def test_model_kwargs_carries_profile_kwargs_and_fallbacks(self):
        model = worker.build_model(
            "openai/combo/deepseek-main", None, None,
            ["openai/combo/fallback", "anthropic/claude-haiku-4-5"],
            {"temperature": 0},
        )
        self.assertEqual(model.model_kwargs["temperature"], 0)
        self.assertEqual(
            model.model_kwargs["fallbacks"],
            ["openai/combo/fallback", "anthropic/claude-haiku-4-5"],
        )

    def test_no_fallbacks_means_no_fallbacks_key(self):
        model = worker.build_model("openai/combo/deepseek-main", None, None, [], {"temperature": 0.2})
        self.assertNotIn("fallbacks", model.model_kwargs)
        self.assertEqual(model.model_kwargs, {"temperature": 0.2})

    def test_bare_model_strips_legacy_colon_prefix_only(self):
        self.assertEqual(worker._bare_model("litellm:openai/combo-deepseek-main"), "openai/combo-deepseek-main")
        self.assertEqual(worker._bare_model("no-prefix-model"), "no-prefix-model")
        # The litellm provider prefix (before the FIRST slash) is never touched.
        self.assertEqual(worker._bare_model("openai/combo/deepseek-main"), "openai/combo/deepseek-main")


class TestGitCommandAllowed(unittest.TestCase):
    def _allowed(self, cmd: str) -> bool:
        ok, _ = worker.git_command_allowed(cmd)
        return ok

    def test_allowed_commands(self):
        for cmd in [
            "git status",
            "git diff",
            "git log --merges",
            "git show HEAD",
            "git add file.py",
            "git blame file.py",
            "git grep TODO",
            "git ls-files",
            "git rev-parse HEAD",
            "git apply patch.diff",
            "git rm file.py",
            "git mv a.py b.py",
            "git restore file.py",
            "git merge-base A B",
            "git branch",
            "git branch --show-current",
            "git branch --list",
            "git branch -a",
            "echo hi",
            "ls -la",
        ]:
            self.assertTrue(self._allowed(cmd), cmd)

    def test_blocked_commands(self):
        for cmd in [
            "git push origin main",
            "git fetch",
            "git pull",
            "git merge feature",
            "git rebase -i HEAD~3",
            "git stash",
            "git tag v1",
            "git remote add x y",
            "git checkout main",
            "git switch main",
            "git worktree add ../x",
            "git branch -D main",
            "git branch new-branch",
            # The server commits, not the worker — commit stopped being a
            # scope-checkable no-op once a committed file could dodge the
            # uncommitted-porcelain scope check (review-fix §C.1).
            "git commit -m 'msg'",
            "git reset",
            "git reset --soft HEAD~1",
            "git reset file.py",
            "git reset --hard",
            "git reset --merge",
            "git reset --keep",
        ]:
            self.assertFalse(self._allowed(cmd), cmd)

    def test_blocked_global_options(self):
        # -c/-C outrank GIT_CONFIG_* and let a worker point git at another
        # repo entirely — blocked outright, even in front of an otherwise
        # allowed subcommand.
        self.assertFalse(self._allowed("git -c protocol.allow=always push"))
        self.assertFalse(self._allowed("git -c a=b status"))
        self.assertFalse(self._allowed("git -C /tmp status"))
        self.assertFalse(self._allowed("git -C . push"))
        # Both the inline and two-token forms.
        self.assertFalse(self._allowed("git --git-dir=/x status"))
        self.assertFalse(self._allowed("git --git-dir /x status"))
        self.assertFalse(self._allowed("git --work-tree=/x status"))
        self.assertFalse(self._allowed("git --work-tree /x status"))
        self.assertFalse(self._allowed("git --exec-path=/x status"))
        self.assertFalse(self._allowed("git --namespace=x status"))
        # The read-only pager options stay allowed.
        self.assertTrue(self._allowed("git --no-pager log"))
        self.assertTrue(self._allowed("git -p log"))
        self.assertTrue(self._allowed("git --paginate log"))

    def test_blocked_global_option_reason_text(self):
        ok, reason = worker.git_command_allowed("git -c protocol.allow=always push")
        self.assertFalse(ok)
        self.assertEqual(reason, "git global option -c is blocked for workers")

    def test_sh_and_bash_recursion(self):
        self.assertFalse(self._allowed('sh -c "git push"'))
        self.assertFalse(self._allowed("bash -c 'git push origin main'"))
        self.assertTrue(self._allowed('sh -c "git status"'))
        # Wrappers, combined flags and substitutions.
        self.assertFalse(self._allowed('sh -lc "git push"'))
        self.assertFalse(self._allowed("env GIT_DIR=x git push"))
        self.assertFalse(self._allowed("echo $(git push)"))
        self.assertFalse(self._allowed("xargs git fetch"))
        self.assertTrue(self._allowed("env git status"))

    def test_chained_commands_glued_or_spaced(self):
        # shlex.split alone would leave "status;git" as one token — the
        # punctuation-aware tokenizer must still catch the chained push.
        self.assertFalse(self._allowed("git status;git push"))
        self.assertFalse(self._allowed("git status && git push"))
        self.assertFalse(self._allowed("git status || git push"))
        self.assertFalse(self._allowed("git status | git push"))
        self.assertTrue(self._allowed("git status && git diff"))

    def test_unparseable_command_is_blocked(self):
        ok, reason = worker.git_command_allowed("echo 'unterminated")
        self.assertFalse(ok)
        self.assertIn("could not parse", reason)

    def test_blocked_message_text(self):
        ok, reason = worker.git_command_allowed("git push origin main")
        self.assertFalse(ok)
        self.assertEqual(
            reason,
            "git push is blocked for workers: you operate on a disposable branch; the supervisor "
            "reviews and merges.",
        )


class TestDriveScanGuard(unittest.TestCase):
    def test_backend_execute_rejects_drive_scan_and_dangerous_git(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            backend = worker.SupervisedShellBackend(
                root_dir=tmp, virtual_mode=True, timeout=5, inherit_env=False, env={},
            )
            result = backend.execute("git push origin main")
            self.assertEqual(result.exit_code, 1)
            self.assertIn("blocked", result.output)

            result = backend.execute("find / -name secret")
            self.assertEqual(result.exit_code, 1)
            self.assertIn("drive root", result.output)

            ok = backend.execute("echo hi")
            self.assertEqual(ok.exit_code, 0)


class TestBuildShellEnv(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_keeps_git_config_and_drops_api_keys(self):
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = "credential.helper"
        os.environ["GIT_TERMINAL_PROMPT"] = "0"
        os.environ["MY_SERVICE_API_KEY"] = "secret"
        os.environ["MONKEY_9ROUTER_KEY"] = "secret2"
        os.environ["MONKEY_WORKER_API_KEY"] = "secret3"
        os.environ["GITHUB_TOKEN"] = "secret4"

        env = worker.build_shell_env("MONKEY_9ROUTER_KEY")

        self.assertEqual(env.get("GIT_CONFIG_COUNT"), "1")
        self.assertEqual(env.get("GIT_CONFIG_KEY_0"), "credential.helper")
        self.assertEqual(env.get("GIT_TERMINAL_PROMPT"), "0")
        self.assertNotIn("MY_SERVICE_API_KEY", env)
        self.assertNotIn("MONKEY_9ROUTER_KEY", env)
        self.assertNotIn("MONKEY_WORKER_API_KEY", env)
        self.assertNotIn("GITHUB_TOKEN", env)


class TestCostTracker(unittest.TestCase):
    def _response(self, model="combo/deepseek-main", prompt=100, completion=50):
        usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion)
        return SimpleNamespace(model=model, usage=usage)

    def test_fallback_pricing_when_litellm_has_no_price(self):
        tracker = worker.CostTracker(price_in=1.0, price_out=2.0, max_tokens_total=None)
        # litellm.completion_cost will raise/return None for this fake response,
        # so the flat per-1M-token fallback should carry the cost.
        tracker(None, self._response(prompt=100, completion=50), None, None)
        self.assertTrue(tracker.priced)
        self.assertAlmostEqual(tracker.cost_usd, 100 * 1.0 / 1e6 + 50 * 2.0 / 1e6)
        self.assertEqual(tracker.prompt_tokens, 100)
        self.assertEqual(tracker.completion_tokens, 50)
        self.assertEqual(tracker.total_tokens, 150)

    def test_unpriced_without_prices_configured(self):
        tracker = worker.CostTracker(price_in=None, price_out=None, max_tokens_total=None)
        tracker(None, self._response(), None, None)
        self.assertFalse(tracker.priced)
        self.assertIsNone(tracker.final_cost_usd())
        self.assertEqual(tracker.total_tokens, 150)

    def test_models_seen_is_ordered_and_deduped(self):
        tracker = worker.CostTracker(price_in=None, price_out=None, max_tokens_total=None)
        tracker(None, self._response(model="combo/a"), None, None)
        tracker(None, self._response(model="combo/b"), None, None)
        tracker(None, self._response(model="combo/a"), None, None)
        self.assertEqual(tracker.models_seen, ["combo/a", "combo/b"])

    def test_token_cap_is_observable_after_enough_calls(self):
        tracker = worker.CostTracker(price_in=None, price_out=None, max_tokens_total=200)
        tracker(None, self._response(prompt=100, completion=50), None, None)
        self.assertFalse(tracker.total_tokens > tracker.max_tokens_total)
        tracker(None, self._response(prompt=100, completion=50), None, None)
        self.assertTrue(tracker.total_tokens > tracker.max_tokens_total)


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


class TestSelftest(unittest.TestCase):
    def test_selftest_prints_selftest_ok(self):
        import subprocess

        worker_path = Path(__file__).resolve().parent.parent / "worker.py"
        proc = subprocess.run(
            ["uv", "run", str(worker_path), "--selftest"],
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SELFTEST_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
