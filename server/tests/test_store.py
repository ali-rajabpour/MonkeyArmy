"""Configuration-facade store tests — stdlib only, no mcp import."""

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
        # Shield the tests from real user env.
        self._saved = {k: os.environ.pop(k, None) for k in ("MONKEY_WORKER_API_KEY", "TEST_PROV_KEY")}

    def tearDown(self):
        os.environ.pop("MONKEY_ARMY_HOME", None)
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
        self._tmp.cleanup()


class TestProfiles(StoreTestCase):
    def test_set_and_load_round_trip(self):
        store.set_profile("dc", "litellm:openai/combo-deepseek", "MONKEY_9ROUTER_KEY")
        s = store.load_store()
        self.assertEqual(s["profiles"]["dc"]["model"], "litellm:openai/combo-deepseek")
        self.assertEqual(s["default_profile"], "dc")  # first profile becomes default

    def test_model_validation(self):
        with self.assertRaises(ValueError):
            store.set_profile("bad", "no-prefix-model")

    def test_fallback_models_round_trip(self):
        store.set_profile(
            "dc", "litellm:openai/combo-deepseek", "MONKEY_9ROUTER_KEY",
            fallback_models=["litellm:openai/combo-fallback", "litellm:anthropic/claude-haiku-4-5"],
        )
        s = store.load_store()
        self.assertEqual(
            s["profiles"]["dc"]["fallback_models"],
            ["litellm:openai/combo-fallback", "litellm:anthropic/claude-haiku-4-5"],
        )

    def test_fallback_models_validated_like_the_primary_model(self):
        with self.assertRaises(ValueError):
            store.set_profile(
                "dc", "litellm:openai/combo-deepseek", fallback_models=["no-prefix-model"],
            )

    def test_empty_fallback_models_not_persisted(self):
        store.set_profile("dc", "litellm:openai/combo-deepseek", fallback_models=[])
        s = store.load_store()
        self.assertNotIn("fallback_models", s["profiles"]["dc"])

    def test_remove_reassigns_default(self):
        store.set_profile("a", "litellm:x/y")
        store.set_profile("b", "litellm:x/z")
        self.assertTrue(store.remove_profile("a"))
        self.assertEqual(store.load_store()["default_profile"], "b")
        self.assertFalse(store.remove_profile("a"))

    def test_set_default_unknown_raises(self):
        with self.assertRaises(KeyError):
            store.set_default_profile("nope")


class TestResolution(StoreTestCase):
    ENV_DEFAULTS = {"model": "litellm:env/model", "api_key_env_var": "ENV_KEY_VAR"}

    def test_no_store_falls_back_to_env(self):
        r = store.resolve_profile(None, self.ENV_DEFAULTS)
        self.assertEqual(r["model"], "litellm:env/model")
        self.assertIn("legacy", r["source"])
        self.assertEqual(r["fallback_models"], [])  # always present, never None

    def test_resolve_surfaces_fallback_models(self):
        store.set_profile(
            "dc", "litellm:openai/combo-deepseek",
            fallback_models=["litellm:openai/combo-fallback"],
        )
        r = store.resolve_profile("dc", self.ENV_DEFAULTS)
        self.assertEqual(r["fallback_models"], ["litellm:openai/combo-fallback"])

    def test_default_profile_wins_over_env(self):
        store.set_profile("dc", "litellm:openai/combo-deepseek", "MONKEY_9ROUTER_KEY")
        r = store.resolve_profile(None, self.ENV_DEFAULTS)
        self.assertEqual(r["model"], "litellm:openai/combo-deepseek")
        self.assertIn("default", r["source"])

    def test_unknown_profile_lists_available(self):
        store.set_profile("dc", "litellm:x/y")
        with self.assertRaises(KeyError) as cm:
            store.resolve_profile("nope", self.ENV_DEFAULTS)
        self.assertIn("dc", str(cm.exception))

    def test_key_resolution_precedence(self):
        store.set_profile("p", "litellm:x/y", "TEST_PROV_KEY")
        # 1. credentials file wins
        store.store_credential("TEST_PROV_KEY", "from-credentials")
        os.environ["TEST_PROV_KEY"] = "from-env"
        r = store.resolve_profile("p", self.ENV_DEFAULTS)
        self.assertEqual(r["api_key"], "from-credentials")
        # 2. env var when no credential
        Path(store.credentials_path()).unlink()
        r = store.resolve_profile("p", self.ENV_DEFAULTS)
        self.assertEqual(r["api_key"], "from-env")
        # 3. legacy MONKEY_WORKER_API_KEY as last resort
        del os.environ["TEST_PROV_KEY"]
        os.environ["MONKEY_WORKER_API_KEY"] = "legacy"
        r = store.resolve_profile("p", self.ENV_DEFAULTS)
        self.assertEqual(r["api_key"], "legacy")
        del os.environ["MONKEY_WORKER_API_KEY"]
        # 4. nothing -> None (keyless profiles run without one)
        r = store.resolve_profile("p", self.ENV_DEFAULTS)
        self.assertIsNone(r["api_key"])


class TestAuthState(StoreTestCase):
    def test_key_availability(self):
        store.set_profile("p", "litellm:x/y", "TEST_PROV_KEY")
        prof = store.load_store()["profiles"]["p"]
        self.assertFalse(store.auth_state(prof)["api_key_available"])
        store.store_credential("TEST_PROV_KEY", "k")
        self.assertTrue(store.auth_state(prof)["api_key_available"])

    def test_corrupt_config_degrades_to_empty(self):
        store.config_path().parent.mkdir(parents=True, exist_ok=True)
        store.config_path().write_text("{not json", encoding="utf-8")
        s = store.load_store()
        self.assertEqual(s["profiles"], {})


if __name__ == "__main__":
    unittest.main()
