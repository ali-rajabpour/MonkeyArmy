"""Configuration-facade store tests — stdlib only, no mcp import."""

import os
import stat
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["MONKEY_ARMY_HOME"] = self._tmp.name
        # Shield the tests from real user env.
        self._saved = {k: os.environ.pop(k, None) for k in ("TEST_PROV_KEY",)}

    def tearDown(self):
        os.environ.pop("MONKEY_ARMY_HOME", None)
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
        self._tmp.cleanup()


class TestProfiles(StoreTestCase):
    def test_set_and_load_round_trip(self):
        store.set_profile(
            "dc", "openai/combo/deepseek-main", "MONKEY_9ROUTER_KEY",
            api_base="http://100.64.0.1/v1",
            price_per_mtok={"input": 0.27, "output": 1.10},
            model_kwargs={"temperature": 0},
            limits={"max_budget_usd": 0.5},
        )
        s = store.load_store()
        prof = s["profiles"]["dc"]
        self.assertEqual(prof["model"], "openai/combo/deepseek-main")
        self.assertEqual(prof["api_base"], "http://100.64.0.1/v1")
        self.assertEqual(prof["price_per_mtok"], {"input": 0.27, "output": 1.10})
        self.assertEqual(prof["model_kwargs"], {"temperature": 0})
        self.assertEqual(prof["limits"], {"max_budget_usd": 0.5})
        self.assertEqual(s["default_profile"], "dc")  # first profile becomes default

    def test_model_validation_requires_slash(self):
        with self.assertRaises(ValueError):
            store.set_profile("bad", "no-slash-model")

    def test_legacy_litellm_prefix_rejected_with_guidance(self):
        with self.assertRaises(ValueError) as cm:
            store.set_profile("bad", "litellm:openai/combo-deepseek")
        self.assertIn("litellm:", str(cm.exception))
        self.assertIn("drop", str(cm.exception))

    def test_api_base_must_be_http(self):
        with self.assertRaises(ValueError):
            store.set_profile("bad", "openai/combo/x", api_base="ftp://nope")

    def test_negative_price_rejected(self):
        with self.assertRaises(ValueError):
            store.set_profile("bad", "openai/combo/x", price_per_mtok={"input": -1})

    def test_non_positive_limit_rejected(self):
        with self.assertRaises(ValueError):
            store.set_profile("bad", "openai/combo/x", limits={"max_budget_usd": 0})

    def test_fallback_models_round_trip(self):
        store.set_profile(
            "dc", "openai/combo/deepseek-main", "MONKEY_9ROUTER_KEY",
            fallback_models=["openai/combo/fallback", "anthropic/claude-haiku-4-5"],
        )
        s = store.load_store()
        self.assertEqual(
            s["profiles"]["dc"]["fallback_models"],
            ["openai/combo/fallback", "anthropic/claude-haiku-4-5"],
        )

    def test_fallback_models_validated_like_the_primary_model(self):
        with self.assertRaises(ValueError):
            store.set_profile(
                "dc", "openai/combo/deepseek-main", fallback_models=["no-slash-model"],
            )

    def test_empty_fallback_models_not_persisted(self):
        store.set_profile("dc", "openai/combo/deepseek-main", fallback_models=[])
        s = store.load_store()
        self.assertNotIn("fallback_models", s["profiles"]["dc"])

    def test_remove_reassigns_default(self):
        store.set_profile("a", "openai/x/y")
        store.set_profile("b", "openai/x/z")
        self.assertTrue(store.remove_profile("a"))
        self.assertEqual(store.load_store()["default_profile"], "b")
        self.assertFalse(store.remove_profile("a"))

    def test_set_default_unknown_raises(self):
        with self.assertRaises(KeyError):
            store.set_default_profile("nope")


class TestCredentialsFileMode(StoreTestCase):
    def test_credentials_file_is_0600(self):
        store.store_credential("TEST_PROV_KEY", "secret")
        mode = stat.S_IMODE(os.stat(store.credentials_path()).st_mode)
        self.assertEqual(mode, 0o600)

    def test_config_file_is_also_0600(self):
        store.set_profile("p", "openai/x/y")
        mode = stat.S_IMODE(os.stat(store.config_path()).st_mode)
        self.assertEqual(mode, 0o600)


class TestResolution(StoreTestCase):
    def test_no_profiles_raises(self):
        with self.assertRaises(KeyError):
            store.resolve_profile()

    def test_resolve_surfaces_fallback_models(self):
        store.set_profile(
            "dc", "openai/combo/deepseek-main",
            fallback_models=["openai/combo/fallback"],
        )
        r = store.resolve_profile("dc")
        self.assertEqual(r["fallback_models"], ["openai/combo/fallback"])

    def test_default_profile_used_when_none_named(self):
        store.set_profile("dc", "openai/combo/deepseek-main", "MONKEY_9ROUTER_KEY")
        r = store.resolve_profile(None)
        self.assertEqual(r["model"], "openai/combo/deepseek-main")
        self.assertIn("default", r["source"])
        self.assertEqual(r["name"], "dc")

    def test_unknown_profile_lists_available(self):
        store.set_profile("dc", "openai/x/y")
        with self.assertRaises(KeyError) as cm:
            store.resolve_profile("nope")
        self.assertIn("dc", str(cm.exception))

    def test_key_resolution_precedence(self):
        store.set_profile("p", "openai/x/y", "TEST_PROV_KEY")
        # 1. credentials file wins
        store.store_credential("TEST_PROV_KEY", "from-credentials")
        os.environ["TEST_PROV_KEY"] = "from-env"
        r = store.resolve_profile("p")
        self.assertEqual(r["api_key"], "from-credentials")
        # 2. env var when no credential
        Path(store.credentials_path()).unlink()
        r = store.resolve_profile("p")
        self.assertEqual(r["api_key"], "from-env")
        # 3. nothing -> None (no legacy fallback; keyless profiles run without one)
        del os.environ["TEST_PROV_KEY"]
        r = store.resolve_profile("p")
        self.assertIsNone(r["api_key"])

    def test_limits_merge_over_defaults(self):
        store.set_profile("p", "openai/x/y", limits={"max_budget_usd": 1.23})
        r = store.resolve_profile("p")
        self.assertEqual(r["limits"]["max_budget_usd"], 1.23)
        self.assertEqual(r["limits"]["recursion_limit_micro"], 80)  # from Defaults, untouched

    def test_defaults_json_overrides_limits(self):
        store.set_defaults({"max_budget_usd": 9.0})
        store.set_profile("p", "openai/x/y")
        r = store.resolve_profile("p")
        self.assertEqual(r["limits"]["max_budget_usd"], 9.0)


class TestAuthState(StoreTestCase):
    def test_key_availability(self):
        store.set_profile("p", "openai/x/y", "TEST_PROV_KEY")
        prof = store.load_store()["profiles"]["p"]
        self.assertFalse(store.auth_state(prof)["api_key_available"])
        store.store_credential("TEST_PROV_KEY", "k")
        self.assertTrue(store.auth_state(prof)["api_key_available"])

    def test_corrupt_config_degrades_to_empty(self):
        store.config_path().parent.mkdir(parents=True, exist_ok=True)
        store.config_path().write_text("{not json", encoding="utf-8")
        s = store.load_store()
        self.assertEqual(s["profiles"], {})


class TestDefaults(StoreTestCase):
    def test_get_defaults_empty_by_default(self):
        self.assertEqual(store.get_defaults(), {})

    def test_set_defaults_merges(self):
        store.set_defaults({"max_diff_lines": 500})
        store.set_defaults({"integrate_mode": "stage"})
        self.assertEqual(
            store.get_defaults(), {"max_diff_lines": 500, "integrate_mode": "stage"}
        )


class TestDoctorRun(StoreTestCase):
    def test_record_sets_meta_last_doctor_at(self):
        self.assertEqual(store.load_store()["meta"], {})
        recorded = store.record_doctor_run()
        self.assertEqual(store.load_store()["meta"]["last_doctor_at"], recorded)

    def test_second_run_overwrites_the_timestamp(self):
        first = store.record_doctor_run()
        second = store.record_doctor_run()
        self.assertGreaterEqual(second, first)
        self.assertEqual(store.load_store()["meta"]["last_doctor_at"], second)


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


class TestResetConfig(unittest.TestCase):
    """A wrong answer in the setup wizard must be recoverable without the user
    hand-deleting files (§ wizard reset)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"MONKEY_ARMY_HOME": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_reset_removes_config_and_credentials(self):
        store.set_profile("p", "openai/combo/x", api_key_env_var="K")
        store.store_credential("K", "sk-secret")
        self.assertTrue(store.config_path().exists())
        self.assertTrue(store.credentials_path().exists())

        removed = store.reset_config()
        self.assertEqual(removed, {"config_removed": True, "credentials_removed": True})
        self.assertFalse(store.config_path().exists())
        self.assertFalse(store.credentials_path().exists())
        self.assertEqual(store.load_store()["profiles"], {})

    def test_reset_is_idempotent(self):
        self.assertEqual(
            store.reset_config(), {"config_removed": False, "credentials_removed": False}
        )

    def test_reset_keeps_repo_state(self):
        repo_state = store.home_dir() / "repos"
        repo_state.mkdir(parents=True, exist_ok=True)
        (repo_state / "keep.txt").write_text("work in flight", encoding="utf-8")
        store.set_profile("p", "openai/combo/x")
        store.reset_config()
        self.assertTrue((repo_state / "keep.txt").exists())


if __name__ == "__main__":
    unittest.main()
