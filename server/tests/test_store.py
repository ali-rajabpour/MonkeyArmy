"""store.py tests — server-written state only (notes, repos index, meta,
prune). Profile/credentials/defaults CRUD moved to env-only config.py
(env-config-spec.md) — see test_config.py for that."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["MONKEY_ARMY_HOME"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("MONKEY_ARMY_HOME", None)
        self._tmp.cleanup()


class TestDoctorRun(StoreTestCase):
    def test_record_sets_meta_last_doctor_at(self):
        self.assertIsNone(store.last_doctor_at())
        recorded = store.record_doctor_run()
        self.assertEqual(store.last_doctor_at(), recorded)

    def test_second_run_overwrites_the_timestamp(self):
        first = store.record_doctor_run()
        second = store.record_doctor_run()
        self.assertGreaterEqual(second, first)
        self.assertEqual(store.last_doctor_at(), second)

    def test_meta_lives_in_meta_json_not_config_json(self):
        store.record_doctor_run()
        self.assertTrue(store.meta_path().exists())
        self.assertEqual(store.meta_path().name, "meta.json")


class TestReposIndex(StoreTestCase):
    def test_remember_and_slug_and_state_dir(self):
        with tempfile.TemporaryDirectory() as repo:
            slug = store.remember_repo(repo)
            self.assertEqual(slug, store.slug_for(repo))
            self.assertIn(str(Path(repo).resolve()), store.all_repos())
            self.assertTrue(
                str(store.repo_state_dir(repo)).endswith(f"repos/{slug}")
                or str(store.repo_state_dir(repo)).endswith(f"repos\\{slug}")
            )


class TestNotes(StoreTestCase):
    def test_append_and_read(self):
        with tempfile.TemporaryDirectory() as repo:
            store.append_note(repo, "tests need uv run pytest -q")
            text = store.read_notes(repo)
            self.assertIn("tests need uv run pytest -q", text)
            self.assertRegex(text, r"^- \d{4}-\d{2}-\d{2}: ")

    def test_read_notes_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as repo:
            self.assertEqual(store.read_notes(repo), "")

    def test_read_notes_max_chars_trims_from_the_end(self):
        with tempfile.TemporaryDirectory() as repo:
            store.append_note(repo, "x" * 100)
            self.assertEqual(len(store.read_notes(repo, max_chars=10)), 10)

    def test_append_refuses_past_4000_chars(self):
        with tempfile.TemporaryDirectory() as repo:
            store.notes_path(repo).parent.mkdir(parents=True, exist_ok=True)
            store.notes_path(repo).write_text("x" * 3990, encoding="utf-8")
            with self.assertRaises(ValueError):
                store.append_note(repo, "this pushes it over the cap")


class TestPruneRepo(StoreTestCase):
    def test_prune_never_raises_on_corrupt_job_file(self):
        with tempfile.TemporaryDirectory() as repo:
            jobs_dir = store.repo_state_dir(repo) / "jobs"
            jobs_dir.mkdir(parents=True)
            (jobs_dir / "bad.json").write_text("{not json", encoding="utf-8")
            result = store.prune_repo(repo, older_than_days=14)
            self.assertEqual(result["jobs_removed"], 0)


if __name__ == "__main__":
    unittest.main()
