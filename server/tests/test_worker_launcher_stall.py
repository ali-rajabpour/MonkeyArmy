"""Stall-watchdog integration tests for worker_launcher.run_worker.

Exercises the REAL run_worker function end-to-end (real subprocess, real
asyncio.wait race between consume() and the stall watchdog) against a tiny
mock "worker" script instead of the real deepagents worker/worker.py — so
these run in a couple seconds without needing a model API key.

`asyncio.create_subprocess_exec` is patched to swap in the mock script while
keeping every other argument (stdin/stdout/stderr/env/...) exactly as
run_worker passes them, so the code under test is unmodified.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from jobs import create_worktree, runtime
from worker_launcher import run_worker

_REAL_CREATE_SUBPROCESS_EXEC = asyncio.create_subprocess_exec


def _write_mock_script(tmpdir: Path, name: str, body: str) -> Path:
    path = tmpdir / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _make_cfg(*, stall_timeout_s: int) -> Config:
    return Config(
        worker_api_key=None, api_key_env_var="X", model="m",
        default_recursion_limit=10, default_rubric_max_iterations=1,
        default_max_budget_usd=5.0, default_timeout_ms=1800000,
        work_dir=".monkey-army", command_timeout_s=30,
        stall_timeout_s=stall_timeout_s,
    )


def _git(cwd: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                    check=True, stdin=subprocess.DEVNULL)


class _MockSubprocessCase(unittest.IsolatedAsyncioTestCase):
    """Shared repo/worktree setup + the create_subprocess_exec patch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = str(Path(self.tmp.name) / "repo")
        Path(self.repo).mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "--allow-empty", "-m", "init")
        self.mock_dir = Path(self.tmp.name) / "mock"
        self.mock_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _job_for(self, base_branch: str = "main") -> dict:
        wt = create_worktree(".monkey-army", self.repo, base_branch)
        return {**wt, "status": "running", "turns": 0, "costUsd": None, "totalTokens": None}

    def _patched_exec(self, script: Path, *, cwd: str | None = None):
        # The real worker resolves file writes against its own root_dir
        # (the worktree), independent of the subprocess's OS-level cwd (which
        # run_worker never sets). The mock script has no such backend, so we
        # give it an explicit cwd here purely to land its plain `open()` calls
        # inside the worktree — that's what salvage_worktree inspects.
        async def fake(*args, **kwargs):  # noqa: ARG001 - swap args[1:] (the "uv ..." cmdline)
            return await _REAL_CREATE_SUBPROCESS_EXEC(
                sys.executable, str(script), cwd=cwd,
                stdin=kwargs["stdin"], stdout=kwargs["stdout"], stderr=kwargs["stderr"],
                env=kwargs["env"], start_new_session=kwargs.get("start_new_session", False),
            )
        return patch("worker_launcher.asyncio.create_subprocess_exec", side_effect=fake)

    def _run_args(self) -> dict:
        return {"spec": "x", "worktree": "", "test_command": None, "definition_of_done": None,
                "recursion_limit": 10, "rubric_max_iterations": 1,
                "model": "m", "api_key_env_var": "X", "api_key": None}


class TestStallWatchdog(_MockSubprocessCase):
    async def test_stall_is_detected_and_salvaged_before_hard_timeout(self):
        script = _write_mock_script(self.mock_dir, "stall.py", """
            import sys, time
            print('PROGRESS:{"step":1,"node":"agent"}', flush=True)
            with open("done_marker.txt", "w") as f:
                f.write("work was actually done before the hang")
            print('PROGRESS:{"step":2,"node":"tools","note":"wrote done_marker.txt"}', flush=True)
            time.sleep(30)  # simulates a hung model call (e.g. rubric grading)
        """)
        job = self._job_for()
        cfg = _make_cfg(stall_timeout_s=1)  # tiny, for a fast test

        with self._patched_exec(script, cwd=job["worktree"]):
            start = asyncio.get_event_loop().time()
            # Hard cap far above the stall timeout: if this fires instead, the
            # stall watchdog isn't doing its job.
            await run_worker(cfg, job, self._run_args(), timeout_ms=60_000)
            elapsed = asyncio.get_event_loop().time() - start

        self.assertEqual(job["status"], "timeout")
        self.assertIn("stalled", job["error"])
        self.assertIn("rubric", job["error"])
        self.assertLess(elapsed, 15, "should be killed by the stall watchdog, not the 60s hard cap")
        self.assertTrue(job.get("salvaged"))
        self.assertIn("done_marker.txt", job.get("filesChanged", []))
        runtime.pop(job["taskId"], None)

    async def test_needs_input_is_exempt_from_the_stall_watchdog(self):
        script = _write_mock_script(self.mock_dir, "ask.py", """
            import time
            print('QUESTION:{"id":"q1","message":"which TTL?"}', flush=True)
            time.sleep(10)  # well past the 1s stall timeout below — must NOT be killed
        """)
        job = self._job_for()
        cfg = _make_cfg(stall_timeout_s=1)

        with self._patched_exec(script):
            task = asyncio.create_task(run_worker(cfg, job, self._run_args(), timeout_ms=60_000))
            await asyncio.sleep(4)  # several stall-check ticks past stall_timeout_s=1
            self.assertEqual(job["status"], "needs_input", "watchdog must not kill a legitimate wait for an answer")

            # Clean up: kill the still-sleeping mock process and stop the task.
            from proc_utils import kill_tree
            pid = job.get("workerPid")
            if pid:
                kill_tree(pid)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        runtime.pop(job["taskId"], None)

    async def test_normal_fast_completion_is_unaffected(self):
        script = _write_mock_script(self.mock_dir, "ok.py", """
            print('PROGRESS:{"step":1,"node":"agent"}', flush=True)
            print('RESULT_JSON:{"status":"succeeded","turns":1,"summary":"done","cost_usd":0.01,"total_tokens":10}')
        """)
        job = self._job_for()
        cfg = _make_cfg(stall_timeout_s=300)  # generous; must not spuriously fire

        with self._patched_exec(script):
            await run_worker(cfg, job, self._run_args(), timeout_ms=30_000)

        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["costUsd"], 0.01)
        runtime.pop(job["taskId"], None)


if __name__ == "__main__":
    unittest.main()
