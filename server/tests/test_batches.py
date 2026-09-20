"""§7.6 batches.py tests — validation/waves are pure; create/status/finish
use real temp git repos (finish drives git_ops.integrate)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import batches
from config import Defaults
from jobs import commit_worktree, create_worktree, persist_job, put_job, stage_files


def _git(cwd: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                           check=check, stdin=subprocess.DEVNULL)


class TestValidate(unittest.TestCase):
    def test_duplicate_keys(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            batches.validate([{"key": "a"}, {"key": "a"}])

    def test_unknown_dependency(self):
        with self.assertRaisesRegex(ValueError, "unknown key"):
            batches.validate([{"key": "a", "dependsOn": ["ghost"]}])

    def test_cycle_detected(self):
        with self.assertRaisesRegex(ValueError, "cycle"):
            batches.validate([{"key": "a", "dependsOn": ["b"]}, {"key": "b", "dependsOn": ["a"]}])

    def test_overlapping_parallel_tasks_rejected(self):
        with self.assertRaisesRegex(ValueError, "overlap"):
            batches.validate([
                {"key": "a", "allowedFiles": ["src/**/*.py"]},
                {"key": "b", "allowedFiles": ["src/utils.py"]},
            ])

    def test_unrestricted_task_gets_its_own_message(self):
        with self.assertRaisesRegex(ValueError, r"task 'a' has no allowedFiles \(unrestricted\)"):
            batches.validate([
                {"key": "a"},
                {"key": "b", "allowedFiles": ["src/b.py"]},
            ])

    def test_disjoint_parallel_tasks_allowed(self):
        waves = batches.validate([
            {"key": "a", "allowedFiles": ["src/a/*.py"]},
            {"key": "b", "allowedFiles": ["src/b/*.py"]},
        ])
        self.assertEqual(waves, [["a", "b"]])

    def test_dependent_tasks_may_overlap_files(self):
        # b depends on a, so they're sequential, not parallel — overlap is fine.
        waves = batches.validate([
            {"key": "a", "allowedFiles": ["src/shared.py"]},
            {"key": "b", "dependsOn": ["a"], "allowedFiles": ["src/shared.py"]},
        ])
        self.assertEqual(waves, [["a"], ["b"]])

    def test_waves_group_independent_tasks_together(self):
        waves = batches.validate([
            {"key": "a", "allowedFiles": ["a.txt"]},
            {"key": "b", "dependsOn": ["a"], "allowedFiles": ["a.txt"]},
            {"key": "c", "allowedFiles": ["c.txt"]},
        ])
        self.assertEqual(waves, [["a", "c"], ["b"]])


class _BatchCase(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        os.environ["MONKEY_ARMY_HOME"] = self._home.name
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = str(Path(self.tmp.name) / "repo")
        Path(self.repo).mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.name", "Ali Real")
        _git(self.repo, "config", "user.email", "ali@example.com")
        _git(self.repo, "commit", "--allow-empty", "-m", "init")
        self.cfg = Defaults(home=Path(self._home.name))

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("MONKEY_ARMY_HOME", None)
        self._home.cleanup()

    def _dispatch_and_succeed(self, key: str, filename: str, approve: bool = True) -> dict:
        """Simulates dispatch_task -> finalize_success -> review_task(approve)
        for one batch task, without spinning up a real worker."""
        wt = create_worktree(self.repo, "main")
        (Path(wt["worktree"]) / filename).write_text(f"{key}\n", encoding="utf-8")
        stage_files(wt["worktree"], [filename])
        commit_worktree(wt["worktree"], f"worker commit for {key}")
        job = {
            **wt, "status": "succeeded", "title": key, "attempt": 1, "model": "test-model",
            "filesChanged": [filename], "costUsd": 0.01, "totalTokens": 100, "priced": True,
            "diffstat": {"added": 1, "removed": 0}, "batchId": None, "batchKey": key,
        }
        if approve:
            job["review"] = {"verdict": "approve", "feedback": None}
        put_job(job)
        persist_job(job)
        return job


class TestCreateStatusLink(_BatchCase):
    def test_create_returns_batch_id_and_waves(self):
        result = batches.create(self.repo, "ship the thing", [
            {"key": "a", "title": "Task A"},
            {"key": "b", "title": "Task B", "dependsOn": ["a"]},
        ])
        self.assertTrue(result["batch_id"].startswith("b_"))
        self.assertEqual(result["order"], [["a"], ["b"]])

    def test_link_then_status_shows_job_fields(self):
        created = batches.create(self.repo, "goal", [{"key": "a", "title": "Task A"}])
        batch_id = created["batch_id"]
        job = self._dispatch_and_succeed("a", "a.txt")
        batches.link(self.repo, batch_id, "a", job["taskId"])

        st = batches.status(self.repo, batch_id)
        self.assertEqual(len(st["tasks"]), 1)
        row = st["tasks"][0]
        self.assertEqual(row["task_id"], job["taskId"])
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["cost_usd"], 0.01)

    def test_link_unknown_batch_raises(self):
        with self.assertRaises(KeyError):
            batches.link(self.repo, "b_nope", "a", "mk_x")


class TestFindManifest(_BatchCase):
    """review-fix §D.5: batch(action='status'|'finish', batch_id) must work
    without repo_path — the skill's own §2.7 call doesn't pass one."""

    def test_finds_manifest_by_batch_id_alone(self):
        from persistence import remember_repo

        remember_repo(self.repo)  # normally done by jobs.create_worktree
        created = batches.create(self.repo, "goal", [{"key": "a", "title": "Task A"}])
        batch_id = created["batch_id"]

        found = batches.find_manifest(batch_id)
        self.assertIsNotNone(found)
        repo, manifest = found
        self.assertEqual(repo, str(Path(self.repo).resolve()))
        self.assertEqual(manifest["batchId"], batch_id)

    def test_unknown_batch_id_returns_none(self):
        self.assertIsNone(batches.find_manifest("b_never_existed"))


class TestFinish(_BatchCase):
    def test_blockers_reported_before_any_integration(self):
        created = batches.create(self.repo, "goal", [
            {"key": "a", "title": "A", "allowedFiles": ["a.txt"]},
            {"key": "b", "title": "B", "allowedFiles": ["b.txt"]},
        ])
        batch_id = created["batch_id"]
        job_a = self._dispatch_and_succeed("a", "a.txt", approve=True)
        batches.link(self.repo, batch_id, "a", job_a["taskId"])
        # b never dispatched — blocker.

        result = batches.finish(self.repo, batch_id, None, "commit", self.cfg)
        self.assertFalse(result["finished"])
        self.assertTrue(any("b" in blocker for blocker in result["blockers"]))
        # Nothing integrated: a's branch/worktree still exist.
        self.assertIn(job_a["branch"], _git(self.repo, "branch", "--list", "monkey/*").stdout)

    def test_finish_integrates_disjoint_tasks_and_reports_totals(self):
        created = batches.create(self.repo, "goal", [
            {"key": "a", "title": "A", "allowedFiles": ["a.txt"]},
            {"key": "b", "title": "B", "allowedFiles": ["b.txt"]},
        ])
        batch_id = created["batch_id"]
        job_a = self._dispatch_and_succeed("a", "a.txt")
        job_b = self._dispatch_and_succeed("b", "b.txt")
        batches.link(self.repo, batch_id, "a", job_a["taskId"])
        batches.link(self.repo, batch_id, "b", job_b["taskId"])

        result = batches.finish(self.repo, batch_id, None, "commit", self.cfg)
        self.assertTrue(result["finished"])
        self.assertEqual(len(result["integrated"]), 2)
        self.assertTrue(all(row["integrated"] for row in result["integrated"]))

        report = result["report"]
        self.assertAlmostEqual(report["workerCostUsd"], 0.02)
        self.assertEqual(report["workerTokens"], 200)
        self.assertTrue(report["priced"])
        self.assertEqual(report["linesAdded"], 2)
        self.assertEqual(report["endState"]["worktreesLeft"], 0)
        self.assertEqual(report["endState"]["branchesLeft"], 0)
        self.assertTrue((Path(self.repo) / "a.txt").exists())
        self.assertTrue((Path(self.repo) / "b.txt").exists())

    def test_status_after_finish_reads_totals_from_the_persisted_record(self):
        """§ bugfix: integrate's cleanup_job drops the task from the
        in-memory registry, so a status() call made AFTER finish() must
        fall back to the minimal persisted job record on disk — which needs
        to carry cost/tokens/diffstat for the numbers to stay non-zero."""
        created = batches.create(self.repo, "goal", [{"key": "a", "title": "A", "allowedFiles": ["a.txt"]}])
        batch_id = created["batch_id"]
        job_a = self._dispatch_and_succeed("a", "a.txt")
        batches.link(self.repo, batch_id, "a", job_a["taskId"])

        result = batches.finish(self.repo, batch_id, None, "commit", self.cfg)
        self.assertTrue(result["finished"])

        from jobs import _jobs
        self.assertNotIn(job_a["taskId"], _jobs, "cleanup_job should have dropped it from the in-memory registry")

        st = batches.status(self.repo, batch_id)
        row = st["tasks"][0]
        self.assertEqual(row["status"], "integrated")
        self.assertEqual(row["cost_usd"], 0.01)
        self.assertGreater(st["total_cost_usd"], 0)

    def test_finish_stops_at_first_unapproved(self):
        created = batches.create(self.repo, "goal", [
            {"key": "a", "title": "A", "allowedFiles": ["a.txt"]},
        ])
        batch_id = created["batch_id"]
        job_a = self._dispatch_and_succeed("a", "a.txt", approve=False)
        batches.link(self.repo, batch_id, "a", job_a["taskId"])

        result = batches.finish(self.repo, batch_id, None, "commit", self.cfg)
        self.assertFalse(result["finished"])
        self.assertIn("not yet approved", result["blockers"][0])


if __name__ == "__main__":
    unittest.main()
