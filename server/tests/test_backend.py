"""§7.7 backend.py tests against a stdlib http.server stub on 127.0.0.1 —
no real network, no real model endpoint (hard rule: never contact a real
model endpoint or 9Router from a test)."""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend


def _profile(api_base: str, **overrides) -> dict:
    prof = {"name": "test", "model": "openai/combo/deepseek-main", "api_base": api_base, "api_key": "k"}
    prof.update(overrides)
    return prof


class _StubServer:
    """Runs `handler_cls` on 127.0.0.1:<random free port> for the life of the
    `with` block."""

    def __init__(self, handler_cls):
        self.httpd = HTTPServer(("127.0.0.1", 0), handler_cls)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()


def _handler(models=None, chat_response=None, chat_status=200, unauthorized=False, stall_s=0):
    """Builds a BaseHTTPRequestHandler subclass serving canned /models and
    /chat/completions responses, without a class-body closure over mutable
    test state (each test gets its own class)."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: A002 - silence test output
            pass

        def _write_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/models":
                self._write_json(200, {"data": [{"id": m} for m in (models or [])]})
            else:
                self._write_json(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/chat/completions":
                self._write_json(404, {"error": "not found"})
                return
            if stall_s:
                time.sleep(stall_s)
            if unauthorized:
                self._write_json(401, {"error": "unauthorized"})
                return
            self._write_json(chat_status, chat_response or {})

    return Handler


class TestDiscoverModels(unittest.TestCase):
    def test_lists_ids_and_combos(self):
        handler = _handler(models=["combo/deepseek-main", "combo/other", "not-a-combo"])
        with _StubServer(handler) as base:
            result = backend.discover_models(_profile(base))
        self.assertEqual(sorted(result["models"]), ["combo/deepseek-main", "combo/other", "not-a-combo"])
        self.assertEqual(sorted(result["combos"]), ["combo/deepseek-main", "combo/other"])

    def test_no_api_base_is_an_error(self):
        result = backend.discover_models(_profile(None))
        self.assertIn("error", result)


class TestProbe(unittest.TestCase):
    def test_tool_calls_present(self):
        chat_response = {
            "model": "combo/deepseek-main",
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            "choices": [{"message": {"tool_calls": [{"id": "1", "function": {"name": "ping"}}]}}],
        }
        handler = _handler(chat_response=chat_response)
        with _StubServer(handler) as base:
            result = backend.probe(_profile(base, name="probe-present"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["tool_calling"], "confirmed")
        self.assertTrue(result["usage_present"])
        self.assertEqual(result["model_reported"], "combo/deepseek-main")

    def test_tool_calls_absent(self):
        chat_response = {"model": "combo/deepseek-main", "choices": [{"message": {"content": "hi"}}]}
        handler = _handler(chat_response=chat_response)
        with _StubServer(handler) as base:
            result = backend.probe(_profile(base, name="probe-absent"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["tool_calling"], "not_observed")
        self.assertFalse(result["usage_present"])

    def test_unauthorized(self):
        handler = _handler(unauthorized=True)
        with _StubServer(handler) as base:
            result = backend.probe(_profile(base, name="probe-401"))
        self.assertFalse(result["ok"])
        self.assertIn("401", result["error"])

    def test_timeout(self):
        handler = _handler(chat_response={"choices": []}, stall_s=1.5)
        with _StubServer(handler) as base:
            result = backend.probe(_profile(base, name="probe-timeout"), timeout_s=0.3)
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_result_is_cached_within_ttl(self):
        handler = _handler(chat_response={"choices": [{"message": {}}]})
        with _StubServer(handler) as base:
            profile = _profile(base, name="probe-cache")
            first = backend.probe(profile, ttl_s=600)
            second = backend.probe(profile, ttl_s=600)
        self.assertEqual(first, second)
        cached = backend.last_probe("probe-cache")
        self.assertIsNotNone(cached)
        self.assertTrue(cached["ok"])


class TestInvalidateProbe(unittest.TestCase):
    def test_named_invalidate_drops_only_that_profile(self):
        handler = _handler(chat_response={"choices": [{"message": {}}]})
        with _StubServer(handler) as base:
            backend.probe(_profile(base, name="inv-a"), ttl_s=600)
            backend.probe(_profile(base, name="inv-b"), ttl_s=600)
        self.assertIsNotNone(backend.last_probe("inv-a"))
        self.assertIsNotNone(backend.last_probe("inv-b"))

        backend.invalidate_probe("inv-a")

        self.assertIsNone(backend.last_probe("inv-a"))
        self.assertIsNotNone(backend.last_probe("inv-b"))

    def test_invalidate_with_no_name_clears_everything(self):
        handler = _handler(chat_response={"choices": [{"message": {}}]})
        with _StubServer(handler) as base:
            backend.probe(_profile(base, name="inv-c"), ttl_s=600)
            backend.probe(_profile(base, name="inv-d"), ttl_s=600)

        backend.invalidate_probe()

        self.assertIsNone(backend.last_probe("inv-c"))
        self.assertIsNone(backend.last_probe("inv-d"))

    def test_invalidate_unknown_profile_is_a_noop(self):
        backend.invalidate_probe("never-probed")  # must not raise


class TestBareModelForHttp(unittest.TestCase):
    def test_strips_provider_prefix(self):
        self.assertEqual(backend._bare_model_for_http("openai/combo/deepseek-main"), "combo/deepseek-main")

    def test_no_prefix_is_unchanged(self):
        self.assertEqual(backend._bare_model_for_http("bare-model"), "bare-model")


if __name__ == "__main__":
    unittest.main()
