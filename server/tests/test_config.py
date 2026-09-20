"""env-config-spec.md: config.py is the single env reader. Every test sets
the environment explicitly (mock.patch.dict) and points MONKEY_ARMY_HOME at
a temp dir — no reliance on the real ~/.monkey-army or any ambient env."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config

REQUIRED_ENV = {
    "MONKEY_9ROUTER_BASE_URL": "http://127.0.0.1:9999/v1",
    "MONKEY_9ROUTER_KEY": "test-key",
    "MONKEY_WORKER_MODEL": "openai/combo/deepseek-main",
}


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        # Shield tests from any MONKEY_* the real shell happens to export.
        self._cleared = {k: os.environ.pop(k, None) for k in list(os.environ) if k.startswith("MONKEY_")}
        os.environ["MONKEY_ARMY_HOME"] = self._home.name

    def tearDown(self):
        for k in list(os.environ):
            if k.startswith("MONKEY_"):
                os.environ.pop(k, None)
        for k, v in self._cleared.items():
            if v is not None:
                os.environ[k] = v
        self._home.cleanup()


class TestRequiredConfigMissing(ConfigTestCase):
    def test_all_three_missing_report_together(self):
        result = config.required_config()
        self.assertEqual(len(result["errors"]), 3)
        joined = "; ".join(result["errors"])
        for name in config.REQUIRED_VARS:
            self.assertIn(name, joined)

    def test_base_url_missing_named(self):
        with mock.patch.dict(os.environ, {"MONKEY_9ROUTER_KEY": "k", "MONKEY_WORKER_MODEL": "openai/x/y"}):
            result = config.required_config()
        self.assertEqual(result["errors"], [
            f"MONKEY_9ROUTER_BASE_URL is not set — export it in the shell you launch Claude Code "
            "from (see .env.example), then restart Claude Code"
        ])

    def test_key_missing_named(self):
        with mock.patch.dict(os.environ, {"MONKEY_9ROUTER_BASE_URL": "http://x/v1", "MONKEY_WORKER_MODEL": "openai/x/y"}):
            result = config.required_config()
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("MONKEY_9ROUTER_KEY", result["errors"][0])

    def test_model_missing_named(self):
        with mock.patch.dict(os.environ, {"MONKEY_9ROUTER_BASE_URL": "http://x/v1", "MONKEY_9ROUTER_KEY": "k"}):
            result = config.required_config()
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("MONKEY_WORKER_MODEL", result["errors"][0])


class TestRequiredConfigInvalid(ConfigTestCase):
    def test_base_url_without_scheme_is_invalid(self):
        with mock.patch.dict(os.environ, {**REQUIRED_ENV, "MONKEY_9ROUTER_BASE_URL": "100.64.0.1/v1"}):
            result = config.required_config()
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("MONKEY_9ROUTER_BASE_URL", result["errors"][0])

    def test_model_without_slash_is_invalid(self):
        with mock.patch.dict(os.environ, {**REQUIRED_ENV, "MONKEY_WORKER_MODEL": "no-slash-model"}):
            result = config.required_config()
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("MONKEY_WORKER_MODEL", result["errors"][0])

    def test_legacy_litellm_prefix_rejected(self):
        with mock.patch.dict(os.environ, {**REQUIRED_ENV, "MONKEY_WORKER_MODEL": "litellm:openai/combo-deepseek"}):
            result = config.required_config()
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("litellm:", result["errors"][0])

    def test_valid_config_resolves_with_no_errors(self):
        with mock.patch.dict(os.environ, REQUIRED_ENV):
            result = config.required_config()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["base_url"], REQUIRED_ENV["MONKEY_9ROUTER_BASE_URL"])
        self.assertEqual(result["api_key"], REQUIRED_ENV["MONKEY_9ROUTER_KEY"])
        self.assertEqual(result["model"], REQUIRED_ENV["MONKEY_WORKER_MODEL"])
        self.assertEqual(result["fallback_models"], [])
        self.assertEqual(result["prices"], {})
        self.assertEqual(result["model_kwargs"], {})


class TestOptionalWorkerSettings(ConfigTestCase):
    def test_fallback_models_parsed_from_csv(self):
        with mock.patch.dict(os.environ, {**REQUIRED_ENV, "MONKEY_FALLBACK_MODELS": "openai/a/b, openai/c/d"}):
            result = config.required_config()
        self.assertEqual(result["fallback_models"], ["openai/a/b", "openai/c/d"])

    def test_prices_parsed(self):
        with mock.patch.dict(os.environ, {
            **REQUIRED_ENV, "MONKEY_PRICE_INPUT_PER_MTOK": "0.27", "MONKEY_PRICE_OUTPUT_PER_MTOK": "1.10",
        }):
            result = config.required_config()
        self.assertEqual(result["prices"], {"input": 0.27, "output": 1.10})

    def test_invalid_price_is_an_error(self):
        with mock.patch.dict(os.environ, {**REQUIRED_ENV, "MONKEY_PRICE_INPUT_PER_MTOK": "not-a-number"}):
            result = config.required_config()
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("MONKEY_PRICE_INPUT_PER_MTOK", result["errors"][0])

    def test_model_kwargs_json_parsed(self):
        with mock.patch.dict(os.environ, {**REQUIRED_ENV, "MONKEY_MODEL_KWARGS_JSON": '{"temperature": 0}'}):
            result = config.required_config()
        self.assertEqual(result["model_kwargs"], {"temperature": 0})

    def test_invalid_model_kwargs_json_is_an_error(self):
        with mock.patch.dict(os.environ, {**REQUIRED_ENV, "MONKEY_MODEL_KWARGS_JSON": "{not json"}):
            result = config.required_config()
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("MONKEY_MODEL_KWARGS_JSON", result["errors"][0])


class TestLoadDefaults(ConfigTestCase):
    def test_defaults_unset_uses_documented_values(self):
        d = config.load_defaults()
        self.assertEqual(d.max_diff_lines, 300)
        self.assertEqual(d.integrate_mode, "commit")
        self.assertEqual(d.max_budget_usd, 0.50)
        self.assertEqual(d.wait_hard_cap_s, 170)

    def test_int_override_applies(self):
        with mock.patch.dict(os.environ, {"MONKEY_MAX_DIFF_LINES": "500"}):
            self.assertEqual(config.load_defaults().max_diff_lines, 500)

    def test_float_override_applies(self):
        with mock.patch.dict(os.environ, {"MONKEY_MAX_BUDGET_USD": "2.5"}):
            self.assertEqual(config.load_defaults().max_budget_usd, 2.5)

    def test_bad_int_names_the_variable(self):
        with mock.patch.dict(os.environ, {"MONKEY_MAX_DIFF_LINES": "abc"}):
            with self.assertRaises(ValueError) as cm:
                config.load_defaults()
        self.assertIn("MONKEY_MAX_DIFF_LINES", str(cm.exception))

    def test_non_positive_int_rejected(self):
        with mock.patch.dict(os.environ, {"MONKEY_MAX_DIFF_LINES": "0"}):
            with self.assertRaises(ValueError):
                config.load_defaults()

    def test_bad_integrate_mode_names_the_variable(self):
        with mock.patch.dict(os.environ, {"MONKEY_INTEGRATE_MODE": "squash"}):
            with self.assertRaises(ValueError) as cm:
                config.load_defaults()
        self.assertIn("MONKEY_INTEGRATE_MODE", str(cm.exception))

    def test_integrate_mode_stage_accepted(self):
        with mock.patch.dict(os.environ, {"MONKEY_INTEGRATE_MODE": "stage"}):
            self.assertEqual(config.load_defaults().integrate_mode, "stage")

    def test_wait_hard_cap_not_configurable(self):
        # No MONKEY_WAIT_HARD_CAP_S variable exists at all — it's fixed at 170.
        with mock.patch.dict(os.environ, {"MONKEY_WAIT_TIMEOUT_S": "5"}):
            d = config.load_defaults()
        self.assertEqual(d.wait_timeout_s, 5)
        self.assertEqual(d.wait_hard_cap_s, 170)


class TestHomeDir(ConfigTestCase):
    def test_override_and_default(self):
        self.assertEqual(config.home_dir(), Path(self._home.name))
        os.environ.pop("MONKEY_ARMY_HOME")
        self.assertEqual(config.home_dir(), Path.home() / ".monkey-army")


if __name__ == "__main__":
    unittest.main()
