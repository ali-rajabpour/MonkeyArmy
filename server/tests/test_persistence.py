"""Persistence unit tests — proves the on-disk format matches the TypeScript
server's (src/persistence.ts) so jobs written by one implementation load in
the other. Stdlib only, no mcp import."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from persistence import (
    delete_persisted_job,
    find_persisted_job,
    job_file_path,
    load_job,
    remember_repo,
    save_job,
    serialize_job,
)

# Byte-for-byte example of what the TypeScript server persists
# (serializeJob: JSON.stringify(rest, null, 2) with camelCase keys).
TS_FIXTURE = {
    "taskId": "mk_abc123_x1y2z3",
    "status": "succeeded",
    "progress": "agent#42",
    "turns": 77,
    "costUsd": 0.1234,
    "totalTokens": 45678,
    "summary": "did the thing",
    "patchPath": "C:\\repo\\.monkey-army\\patches\\mk_abc123_x1y2z3.diff",
    "filesChanged": ["math.js", "test.js"],
    "branch": "monkey/mk_abc123_x1y2z3",
    "worktree": "C:\\repo\\.monkey-army\\worktrees\\mk_abc123_x1y2z3",
    "repo": "C:\\repo",
}


class TestPersistenceFormat(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _fixture_job(self):
        job = dict(TS_FIXTURE)
        job["repo"] = self.repo
        return job

    def test_round_trip_preserves_all_fields(self):
        job = self._fixture_job()
        save_job(job, ".monkey-army")
        loaded = load_job(self.repo, job["taskId"], ".monkey-army")
        self.assertEqual(loaded, job)

    def test_reads_a_ts_written_file(self):
        """A file written exactly the way src/persistence.ts writes it loads fine."""
        job = self._fixture_job()
        path = job_file_path(self.repo, job["taskId"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(job, indent=2), encoding="utf-8")
        loaded = load_job(self.repo, job["taskId"])
        self.assertEqual(loaded["costUsd"], 0.1234)
        self.assertEqual(loaded["filesChanged"], ["math.js", "test.js"])

    def test_serialize_excludes_runtime_handles(self):
        job = self._fixture_job()
        job["abort"] = object()  # runtime-only, must never be serialized
        data = json.loads(serialize_job(job))
        self.assertNotIn("abort", data)
        self.assertEqual(data["taskId"], job["taskId"])

    def test_keys_are_camel_case(self):
        data = json.loads(serialize_job(self._fixture_job()))
        for key in ("taskId", "costUsd", "totalTokens", "patchPath", "filesChanged"):
            self.assertIn(key, data)
        for wrong in ("task_id", "cost_usd", "total_tokens", "patch_path", "files_changed"):
            self.assertNotIn(wrong, data)

    def test_load_missing_returns_none(self):
        self.assertIsNone(load_job(self.repo, "t_nope_000000"))

    def test_delete_is_idempotent(self):
        job = self._fixture_job()
        save_job(job)
        delete_persisted_job(job)
        delete_persisted_job(job)  # second call: file already gone, no raise
        self.assertIsNone(load_job(self.repo, job["taskId"]))

    def test_find_persisted_job_scans_known_repos(self):
        job = self._fixture_job()
        save_job(job)
        remember_repo(self.repo)
        found = find_persisted_job(job["taskId"])
        self.assertIsNotNone(found)
        self.assertEqual(found["taskId"], job["taskId"])


if __name__ == "__main__":
    unittest.main()
