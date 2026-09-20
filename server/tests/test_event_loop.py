"""Regression test for the event-loop-blocking fix (review-fix section A):
several `async` tools used to call blocking subprocess/HTTP code directly on
the loop, which stalls wait_for_tasks (and worker stdout consumption) for
the whole duration. The fix routes those calls through
`loop.run_in_executor` (main.py's `_offload`). This proves the pattern: a
2s blocking function run via run_in_executor must not add its 2s onto a
concurrently running wait_for_tasks call."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import _jobs, put_job, wait_for_tasks


def _blocking_sleep(seconds: float) -> str:
    time.sleep(seconds)  # simulates a blocking subprocess/HTTP call
    return "done"


class TestOffloadDoesNotBlockWait(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        os.environ["MONKEY_ARMY_HOME"] = self._home.name

    def tearDown(self):
        _jobs.clear()
        os.environ.pop("MONKEY_ARMY_HOME", None)
        self._home.cleanup()

    async def test_wait_still_ticks_during_offloaded_blocking_call(self):
        put_job({"taskId": "mk_offload", "status": "running"})

        loop = asyncio.get_running_loop()
        offloaded = loop.run_in_executor(None, _blocking_sleep, 2)

        start = time.time()
        result = await wait_for_tasks(["mk_offload"], 1, wait_timeout_s=10, hard_cap_s=170)
        elapsed = time.time() - start

        # wait_for_tasks' own 1s budget, not budget + the 2s blocking call —
        # proves the executor thread never held up the loop's 1s ticks.
        self.assertLess(elapsed, 1.9)
        self.assertFalse(result["changed"])

        self.assertEqual(await offloaded, "done")


if __name__ == "__main__":
    unittest.main()
