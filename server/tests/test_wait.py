"""§6.2 wait_for_tasks tests. The logic lives in jobs.py (stdlib) precisely
so it's testable here without importing main.py, which needs the `mcp`
package this test suite must run without."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import _jobs, put_job, wait_for_tasks


class TestWaitForTasks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        os.environ["MONKEY_ARMY_HOME"] = self._home.name

    def tearDown(self):
        _jobs.clear()
        os.environ.pop("MONKEY_ARMY_HOME", None)
        self._home.cleanup()

    async def test_immediate_return_on_needs_input(self):
        put_job({"taskId": "mk_a", "status": "needs_input", "question": {"id": "q1", "message": "which TTL?"}})
        result = await wait_for_tasks(["mk_a"], None, wait_timeout_s=10, hard_cap_s=170)
        self.assertEqual(result["elapsed_s"], 0)
        self.assertFalse(result["changed"])
        self.assertEqual(result["tasks"][0]["question"]["id"], "q1")

    async def test_immediate_return_on_terminal(self):
        put_job({"taskId": "mk_b", "status": "succeeded", "costUsd": 0.1})
        result = await wait_for_tasks(["mk_b"], None, wait_timeout_s=10, hard_cap_s=170)
        self.assertEqual(result["elapsed_s"], 0)
        self.assertTrue(result["tasks"][0]["done"])
        self.assertEqual(result["tasks"][0]["cost_usd"], 0.1)

    async def test_returns_when_status_changes_mid_wait(self):
        job = {"taskId": "mk_c", "status": "running"}
        put_job(job)

        async def flip_after_delay():
            await asyncio.sleep(1.2)
            job["status"] = "succeeded"

        asyncio.create_task(flip_after_delay())
        result = await wait_for_tasks(["mk_c"], 10, wait_timeout_s=10, hard_cap_s=170)
        self.assertTrue(result["changed"])
        self.assertEqual(result["tasks"][0]["status"], "succeeded")
        self.assertGreaterEqual(result["elapsed_s"], 1)

    async def test_timeout_elapses_without_a_change(self):
        put_job({"taskId": "mk_d", "status": "running"})
        result = await wait_for_tasks(["mk_d"], 2, wait_timeout_s=10, hard_cap_s=170)
        self.assertFalse(result["changed"])
        self.assertGreaterEqual(result["elapsed_s"], 2)

    async def test_timeout_s_is_capped_at_hard_cap(self):
        put_job({"taskId": "mk_e", "status": "running"})
        # timeout_s far above hard_cap_s must not make the call actually wait
        # that long — a tiny hard_cap proves it's the effective bound.
        result = await wait_for_tasks(["mk_e"], 999, wait_timeout_s=10, hard_cap_s=1)
        self.assertLess(result["elapsed_s"], 3)

    async def test_default_timeout_is_wait_timeout_s(self):
        put_job({"taskId": "mk_f", "status": "running"})
        result = await wait_for_tasks(["mk_f"], None, wait_timeout_s=1, hard_cap_s=170)
        self.assertGreaterEqual(result["elapsed_s"], 1)
        self.assertLess(result["elapsed_s"], 3)

    async def test_unknown_task_id_is_done_immediately(self):
        result = await wait_for_tasks(["mk_never_seen"], None, wait_timeout_s=10, hard_cap_s=170)
        self.assertTrue(result["tasks"][0]["done"])
        self.assertEqual(result["tasks"][0]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
