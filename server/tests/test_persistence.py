"""Persistence unit tests: on-disk job format, repos index, restart
recovery, and the §5.5 status enum. Stdlib only, no mcp import."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from persistence import (
    ACTIVE,
    INTEGRABLE,
    REVIEWABLE,
    TERMINAL,
    all_repos,
    delete_persisted_job,
    find_persisted_job,
    job_file_path,
    load_job,
    remember_repo,
    repo_state_dir,
    save_job,
    serialize_job,
    slug_for,
)

FIXTURE = {
    "taskId": "mk_abc123_x1y2z3",
    "status": "succeeded",
    "progress": "agent#42",
    "turns": 77,
    "costUsd": 0.1234,
    "totalTokens": 45678,
    "summary": "did the thing",
    "filesChanged": ["math.js", "test.js"],
    "branch": "monkey/mk_abc123_x1y2z3",
}


class PersistenceTestCase(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        os.environ["MONKEY_ARMY_HOME"] = self._home.name
        self._repo_tmp = tempfile.TemporaryDirectory()
        self.repo = self._repo_tmp.name

    def tearDown(self):
        os.environ.pop("MONKEY_ARMY_HOME", None)
        self._repo_tmp.cleanup()
        self._home.cleanup()

    def _fixture_job(self):
        job = dict(FIXTURE)
        job["repo"] = self.repo
        return job


class TestStatusEnum(unittest.TestCase):
    def test_disjoint_and_covers_the_documented_set(self):
        self.assertEqual(REVIEWABLE, TERMINAL - {"integrated"})
        self.assertEqual(INTEGRABLE, {"succeeded"})
        self.assertTrue(ACTIVE.isdisjoint(TERMINAL))
        for s in ("running", "needs_input", "verifying"):
            self.assertIn(s, ACTIVE)
        for s in (
            "succeeded", "failed", "failed_verification", "failed_scope",
            "failed_oversized", "timeout", "cancelled", "integrated",
        ):
            self.assertIn(s, TERMINAL)


class TestReposIndex(PersistenceTestCase):
    def test_slug_shape(self):
        slug = slug_for(self.repo)
        self.assertTrue(slug.startswith(Path(self.repo).name + "-"))
        self.assertEqual(len(slug.rsplit("-", 1)[-1]), 8)

    def test_remember_repo_persists_and_all_repos_lists_it(self):
        slug = remember_repo(self.repo)
        self.assertIn(str(Path(self.repo).resolve()), all_repos())
        self.assertEqual(slug, slug_for(self.repo))

    def test_repo_state_dir_is_outside_the_repo(self):
        state_dir = repo_state_dir(self.repo)
        self.assertNotIn(str(Path(self.repo).resolve()), str(state_dir))
        self.assertTrue(str(state_dir).startswith(os.environ["MONKEY_ARMY_HOME"]))


class TestPersistenceFormat(PersistenceTestCase):
    def test_round_trip_preserves_all_fields(self):
        job = self._fixture_job()
        save_job(job)
        loaded = load_job(self.repo, job["taskId"])
        self.assertEqual(loaded, job)

    def test_job_file_lives_under_home_repos_slug_jobs(self):
        job = self._fixture_job()
        path = job_file_path(self.repo, job["taskId"])
        self.assertEqual(path, repo_state_dir(self.repo) / "jobs" / f"{job['taskId']}.json")

    def test_serialize_excludes_runtime_handles(self):
        job = self._fixture_job()
        job["abort"] = object()  # runtime-only, must never be serialized
        data = json.loads(serialize_job(job))
        self.assertNotIn("abort", data)
        self.assertEqual(data["taskId"], job["taskId"])

    def test_keys_are_camel_case(self):
        data = json.loads(serialize_job(self._fixture_job()))
        for key in ("taskId", "costUsd", "totalTokens", "filesChanged"):
            self.assertIn(key, data)
        for wrong in ("task_id", "cost_usd", "total_tokens", "files_changed"):
            self.assertNotIn(wrong, data)

    def test_load_missing_returns_none(self):
        self.assertIsNone(load_job(self.repo, "mk_nope_000000"))

    def test_delete_is_idempotent(self):
        job = self._fixture_job()
        save_job(job)
        delete_persisted_job(job)
        delete_persisted_job(job)  # second call: file already gone, no raise
        self.assertIsNone(load_job(self.repo, job["taskId"]))


class TestRestartRecovery(PersistenceTestCase):
    def test_find_persisted_job_survives_a_fresh_process(self):
        """The scenario that motivated repos.json: a job saved by one
        process must be discoverable by a second that never called
        remember_repo() itself in this run — only repos.json (on disk)
        carries that knowledge across the restart."""
        job = self._fixture_job()
        save_job(job)
        remember_repo(self.repo)

        # Simulate "no in-memory registry": find_persisted_job never
        # consults anything but repos.json + the job files on disk.
        found = find_persisted_job(job["taskId"])
        self.assertIsNotNone(found)
        self.assertEqual(found["taskId"], job["taskId"])

    def test_find_persisted_job_unknown_repo_returns_none(self):
        self.assertIsNone(find_persisted_job("mk_never_seen"))

    def test_job_found_after_clearing_the_in_memory_registry(self):
        """End-to-end restart simulation through jobs.get_job_with_fallback:
        put a job in the in-memory registry, persist it, then clear the
        registry (as a server restart would) and confirm it is still
        reachable via repos.json — the exact bug §7.2 calls out."""
        import jobs

        job = self._fixture_job()
        jobs.put_job(job)
        remember_repo(self.repo)
        save_job(job)
        self.assertIs(jobs.get_job_with_fallback(job["taskId"]), job)

        jobs._jobs.clear()  # simulate the registry being gone after a restart
        found = jobs.get_job_with_fallback(job["taskId"])
        self.assertIsNotNone(found)
        self.assertEqual(found["taskId"], job["taskId"])
        self.assertIsNot(found, job)  # came back from disk, not the old object


if __name__ == "__main__":
    unittest.main()
