"""§7.5 spawn-env tests: sensitive-name filtering, the worker API key and
comm-dir/ask-timeout vars, and the GIT_CONFIG_* neutralisation block.

Also asserts worker_launcher's `is_sensitive_env_name` agrees with worker/
worker.py's own copy — worker.py can't be imported here (it needs litellm/
deepagents, which the stdlib-only server tests never install), so its source
is parsed for the substring tuple instead of executed.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from worker_launcher import (
    _SENSITIVE_ENV_SUBSTRINGS,
    _git_neutralization_env,
    build_spawn_env,
    is_sensitive_env_name,
)

WORKER_PY = Path(__file__).resolve().parent.parent.parent / "worker" / "worker.py"


def _extract_tuple(source: str, name: str) -> tuple[str, ...]:
    idx = source.index(name)
    eq = source.index("=", idx)
    start = source.index("(", eq)
    depth = 0
    end = start
    for i, ch in enumerate(source[start:], start):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    return ast.literal_eval(source[start:end + 1])


def _git(cwd: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                    check=True, stdin=subprocess.DEVNULL)


class TestIsSensitiveEnvName(unittest.TestCase):
    def test_positive_and_negative_names(self):
        for name in ("MONKEY_9ROUTER_API_KEY", "GITHUB_TOKEN", "DB_PASSWORD",
                      "STRIPE_SECRET", "AWS_CREDENTIAL_FILE", "some_apikey"):
            self.assertTrue(is_sensitive_env_name(name), name)
        for name in ("PATH", "HOME", "SystemRoot", "TEMP", "GIT_TERMINAL_PROMPT",
                      "GIT_CONFIG_COUNT", "MONKEY_COMM_DIR", "MONKEY_ASK_TIMEOUT_S"):
            self.assertFalse(is_sensitive_env_name(name), name)

    def test_agrees_with_worker_py_copy(self):
        worker_source = WORKER_PY.read_text(encoding="utf-8")
        worker_substrings = _extract_tuple(worker_source, "_SENSITIVE_ENV_SUBSTRINGS")
        self.assertEqual(worker_substrings, _SENSITIVE_ENV_SUBSTRINGS)

        # Functional cross-check: any name either copy's rule matches must
        # match under the OTHER copy's rule too, computed straight from the
        # parsed tuple (no import of worker.py needed).
        def worker_is_sensitive(name: str) -> bool:
            upper = name.upper()
            return any(token in upper for token in worker_substrings)

        for name in ("MY_API_KEY", "GITHUB_TOKEN", "DB_PASSWORD", "SOME_SECRET",
                      "A_CREDENTIAL_STORE", "PATH", "GIT_CONFIG_KEY_0", "HOME"):
            self.assertEqual(is_sensitive_env_name(name), worker_is_sensitive(name), name)


class TestGitNeutralizationEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        _git(self.repo, "init", "-b", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_remotes(self):
        env = _git_neutralization_env(self.repo)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_CONFIG_COUNT"], "3")
        pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(3)}
        self.assertEqual(pairs["credential.helper"], "")
        self.assertEqual(pairs["protocol.allow"], "never")
        self.assertTrue(pairs["core.askPass"] in ("/usr/bin/false", "false"))

    def test_one_pushurl_and_url_per_remote(self):
        _git(self.repo, "remote", "add", "origin", "https://example.invalid/repo.git")
        _git(self.repo, "remote", "add", "upstream", "https://example.invalid/upstream.git")
        env = _git_neutralization_env(self.repo)
        self.assertEqual(env["GIT_CONFIG_COUNT"], "7")  # 3 base + 2 remotes * 2 keys
        pairs = {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(env["GIT_CONFIG_COUNT"]))
        }
        for remote in ("origin", "upstream"):
            self.assertEqual(pairs[f"remote.{remote}.pushurl"], "monkey-army-blocked://push-disabled")
            self.assertEqual(pairs[f"remote.{remote}.url"], "monkey-army-blocked://fetch-disabled")


class TestBuildSpawnEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        _git(self.repo, "init", "-b", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def test_worker_key_comm_dir_and_ask_timeout_present(self):
        job = {"repo": self.repo, "taskId": "mk_1"}
        args = {"api_key": "sk-secret-value"}
        env = build_spawn_env(job, args, Path("/tmp/comm/mk_1"), ask_timeout_s=600)
        self.assertEqual(env["MONKEY_WORKER_API_KEY"], "sk-secret-value")
        self.assertEqual(env["MONKEY_COMM_DIR"], "/tmp/comm/mk_1")
        self.assertEqual(env["MONKEY_ASK_TIMEOUT_S"], "600")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_ssh_agent_and_stale_git_config_dropped(self):
        from unittest import mock
        job = {"repo": self.repo, "taskId": "mk_1"}
        with mock.patch.dict("os.environ", {"SSH_AUTH_SOCK": "/tmp/agent", "GIT_CONFIG_KEY_9": "x"}):
            env = build_spawn_env(job, {}, Path("/tmp/comm/mk_1"), ask_timeout_s=600)
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertNotIn("GIT_CONFIG_KEY_9", env)
        self.assertEqual(env["GIT_SSH_COMMAND"], "false")

    def test_no_key_means_no_worker_api_key_var(self):
        job = {"repo": self.repo, "taskId": "mk_1"}
        env = build_spawn_env(job, {"api_key": None}, Path("/tmp/comm/mk_1"), ask_timeout_s=600)
        self.assertNotIn("MONKEY_WORKER_API_KEY", env)

    def test_sensitive_parent_env_names_are_dropped(self):
        import os
        os.environ["MONKEY_TEST_SOME_TOKEN"] = "leaky"
        try:
            job = {"repo": self.repo, "taskId": "mk_1"}
            env = build_spawn_env(job, {"api_key": None}, Path("/tmp/comm/mk_1"), ask_timeout_s=600)
            self.assertNotIn("MONKEY_TEST_SOME_TOKEN", env)
        finally:
            del os.environ["MONKEY_TEST_SOME_TOKEN"]


if __name__ == "__main__":
    unittest.main()
