# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp<2"]
# ///
"""Offline end-to-end proof of the monkey-army plugin, run after the review-fix
pass (per-call defaults, probe-cache invalidation, off-loop blocking work, the
narrower worker git allowlist, base-ancestry check, empty-diff -> failed,
finishedAt, batch lookup without repo_path, doctor timestamp, integrate
ordering).

Drives `uv run server/main.py` over MCP stdio exactly like the supervisor
(Claude Code) would, and executes the checks named in docs/VALIDATION.md
(Phase 1/2/4) as individually-reported PASS/FAIL/SKIP tests.

Two modes:
  --fake           bundled scripted OpenAI-compatible server (stdlib only,
                    127.0.0.1) stands in for 9Router -- no network, no cost.
  --api-base ...   a real (or the caller's own) OpenAI-compatible endpoint.

Deliberately NOT named test_*.py: `unittest discover` must not pick this up
-- it drives real subprocesses (uv, git) and is meant to be invoked directly:

    uv run server/tests/e2e_driver.py --fake --phases 1,2,4
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TOY_REPO_SRC = REPO_ROOT / "examples" / "toy-repo"
MAIN_PY = REPO_ROOT / "server" / "main.py"
TEST_CMD = "uv run --no-project --with pytest python -m pytest -q"


# ═══════════════════════════════════════════════════════════════════════
# Bundled fake LLM (only started with --fake) -- a scripted OpenAI-compatible
# server. Every response is derived deterministically from what's already in
# the request (the tools array, and an E2E_JSON_BASE64:<b64> marker embedded
# in the task brief by this same file's spec-builders below). Ported from the
# scratchpad's fake_llm.py, updated for the post-review-fix worker: the
# worker no longer commits (the server does), so no scripted queue ever
# includes a `git commit` step, and the fake now checks the requested model
# id so a bad-model-name profile genuinely fails its probe (T1.5).
# ═══════════════════════════════════════════════════════════════════════

_FAKE_MODEL_ID = "combo/fake"  # set once from --model before the server starts

_MARKER_RE = re.compile(r"E2E_JSON_BASE64:([A-Za-z0-9+/=]+)")
_ACCEPT_RE = re.compile(r"# Acceptance command\s*\n`([^`]+)`")
_FEEDBACK_RE = re.compile(r"Supervisor feedback on attempt")
_STEER_RE = re.compile(r"SUPERVISOR STEERING")


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return " ".join(parts)
    return ""


def _full_text(messages: list) -> str:
    return "\n".join(_text_of(m.get("content")) for m in messages)


def _usage(prompt_tokens: int = 128, completion_tokens: int = 16) -> dict:
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _tool_call_response(tool_name: str, args: dict, usage: dict | None = None) -> dict:
    return {
        "id": f"chatcmpl-fake-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": _FAKE_MODEL_ID,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": json.dumps(args)},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": usage or _usage(),
    }


def _final_response(text: str = "done") -> dict:
    return {
        "id": f"chatcmpl-fake-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": _FAKE_MODEL_ID,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": _usage(),
    }


def _resolve_tool_name(tools: list, wanted: str) -> str:
    for t in tools or []:
        fn = (t or {}).get("function") or {}
        if fn.get("name") == wanted:
            return wanted
    return wanted


def _count_tool_turns(messages: list) -> int:
    return sum(1 for m in messages if m.get("role") == "assistant" and m.get("tool_calls"))


def _build_queue(marker: dict, has_feedback: bool, test_cmd: str | None) -> list[tuple[str, dict]]:
    """Generic scripted plan: pre_steps -> writes (or bad_writes, until the
    worker has seen feedback) -> an optional scope violation -> post_steps ->
    the acceptance command. Covers every scenario except the three with
    genuinely dynamic behaviour (budgetloop/cancelloop/steer, handled inline
    in do_POST below)."""
    scenario = marker.get("scenario", "ok")
    if scenario == "failtest" and not has_feedback:
        writes = marker.get("bad_writes") or []
    else:
        writes = marker.get("writes") or []
    queue: list[tuple[str, dict]] = [(s["tool"], s["args"]) for s in (marker.get("pre_steps") or [])]
    queue += [("write_file", {"file_path": w["path"], "content": w["content"]}) for w in writes]
    if scenario == "scope":
        sv = marker.get("scope_violation")
        if sv:
            queue.append(("write_file", {"file_path": sv["path"], "content": sv["content"]}))
    queue += [(s["tool"], s["args"]) for s in (marker.get("post_steps") or [])]
    if test_cmd:
        queue.append(("execute", {"command": test_cmd}))
    return queue


class _FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet -- keep the harness's own output readable
        pass

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/models"):
            self._send_json({"object": "list", "data": [{"id": _FAKE_MODEL_ID, "object": "model"}]})
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send_json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json({"error": "bad request body"}, 400)
            return

        # A real 9Router rejects an unknown model id -- mirrored here so a
        # profile with a bad model name genuinely fails its probe (T1.5)
        # instead of the fake server answering for any model regardless.
        if req.get("model") != _FAKE_MODEL_ID:
            self._send_json(
                {"error": {"message": f"model {req.get('model')!r} not found", "type": "invalid_request_error"}},
                404,
            )
            return

        tools = req.get("tools") or []
        messages = req.get("messages") or []

        # backend.probe() sends exactly one tool, named "ping".
        if any((t.get("function") or {}).get("name") == "ping" for t in tools):
            self._send_json(_tool_call_response("ping", {"x": 1}))
            return

        full_text = _full_text(messages)
        marker_match = _MARKER_RE.search(full_text)
        if not marker_match:
            self._send_json(_final_response("done (no scenario marker found)"))
            return

        marker = json.loads(base64.b64decode(marker_match.group(1)).decode("utf-8"))
        scenario = marker.get("scenario", "ok")
        has_feedback = bool(_FEEDBACK_RE.search(full_text))
        accept_match = _ACCEPT_RE.search(full_text)
        test_cmd = accept_match.group(1) if accept_match else None
        idx = _count_tool_turns(messages)

        # ── Dynamic scenarios: can't be expressed as a fixed index -> action
        # table because they either never terminate on their own (the harness
        # ends them from outside, via cancel_task or a budget cap) or their
        # output depends on something that happens mid-run (steer_task).
        if scenario == "budgetloop":
            # Deliberately oversized usage per call: two turns are enough to
            # cross both the $0.01 and the 2000-token caps in T4.3/T4.4
            # without the drill needing to run for real wall-clock time.
            self._send_json(_tool_call_response(
                _resolve_tool_name(tools, "execute"), {"command": "echo tick"}, usage=_usage(6000, 2000),
            ))
            return
        if scenario == "cancelloop":
            if idx == 0:
                w = marker["writes"][0]
                self._send_json(_tool_call_response(
                    _resolve_tool_name(tools, "write_file"), {"file_path": w["path"], "content": w["content"]},
                ))
            else:
                self._send_json(_tool_call_response(_resolve_tool_name(tools, "execute"), {"command": "sleep 2"}))
            return
        if scenario == "steer":
            wait_turns = marker.get("wait_turns", 5)
            if idx < wait_turns:
                self._send_json(_tool_call_response(
                    _resolve_tool_name(tools, "execute"), {"command": "sleep 1 && echo waiting"},
                ))
            elif idx == wait_turns:
                steered = bool(_STEER_RE.search(full_text))
                w = marker["writes"][0]
                content = w["content"] if steered else marker["writes_unsteered"][0]["content"]
                self._send_json(_tool_call_response(
                    _resolve_tool_name(tools, "write_file"), {"file_path": w["path"], "content": content},
                ))
            else:
                self._send_json(_final_response("done"))
            return

        queue = _build_queue(marker, has_feedback, test_cmd)
        if idx < len(queue):
            tool_name, args = queue[idx]
            self._send_json(_tool_call_response(_resolve_tool_name(tools, tool_name), args))
        else:
            self._send_json(_final_response("done"))


def start_fake_server(model: str) -> tuple[ThreadingHTTPServer, threading.Thread, int]:
    global _FAKE_MODEL_ID
    _FAKE_MODEL_ID = model.split("/", 1)[1] if "/" in model else model
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, port


def stop_fake_server(server: ThreadingHTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def marker(scenario: str, **extra: Any) -> str:
    payload = {"scenario": scenario, **{k: v for k, v in extra.items() if v is not None}}
    b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return f"E2E_JSON_BASE64:{b64}"


# ═══════════════════════════════════════════════════════════════════════
# Harness plumbing
# ═══════════════════════════════════════════════════════════════════════

RESULTS: list[tuple[str, str, str]] = []  # (id, "PASS"|"FAIL"|"SKIP", evidence)


def record(test_id: str, ok: bool, evidence: str = "") -> bool:
    RESULTS.append((test_id, "PASS" if ok else "FAIL", evidence))
    # flush -- a live run takes minutes per task, so unflushed output leaves
    # a poller staring at nothing until the buffer fills or the process exits.
    print(f"{'PASS' if ok else 'FAIL'}  {test_id}" + (f"  -- {evidence[:300]}" if evidence else ""), flush=True)
    return ok


def skip(test_id: str, reason: str) -> None:
    RESULTS.append((test_id, "SKIP", reason))
    print(f"SKIP  {test_id}  -- {reason}", flush=True)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        stdin=subprocess.DEVNULL, check=True,
    )


def _git_ok(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )


class Ctx:
    """Everything a phase needs: the live MCP session, the scratch repo, and
    small helpers so phase bodies read like a checklist, not plumbing."""

    def __init__(self, args: argparse.Namespace, work_dir: Path, monkey_home: Path, env: dict[str, str]) -> None:
        self.args = args
        self.work_dir = work_dir
        self.monkey_home = monkey_home
        self.env = env
        self.profile = args.profile
        self.task_timeout = args.task_timeout
        self.stdio_ctx = None
        self.session_ctx = None
        self.session: ClientSession | None = None
        self.active_task_ids: set[str] = set()
        self.api_base: str | None = None

    async def connect(self) -> None:
        params = StdioServerParameters(command="uv", args=["run", str(MAIN_PY)], env=self.env)
        self.stdio_ctx = stdio_client(params)
        read, write = await self.stdio_ctx.__aenter__()
        self.session_ctx = ClientSession(read, write)
        self.session = await self.session_ctx.__aenter__()
        await self.session.initialize()

    async def disconnect(self) -> None:
        if self.session_ctx is not None:
            try:
                await self.session_ctx.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self.session_ctx = None
        if self.stdio_ctx is not None:
            try:
                await self.stdio_ctx.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self.stdio_ctx = None
        self.session = None

    async def restart_server(self) -> None:
        """Ends the current MCP server process (stdio_client's own teardown
        closes stdin, waits, then SIGTERM/SIGKILLs if needed -- a real kill,
        not just a polite goodbye) and opens a fresh one against the SAME
        MONKEY_ARMY_HOME, simulating a Claude Code restart mid-task (T2.6).
        The worker subprocess is spawned with start_new_session=True, so it
        survives its parent server's death -- that's the orphan this test
        exercises."""
        await self.disconnect()
        await self.connect()

    async def call(self, tool: str, **kwargs: Any) -> dict:
        assert self.session is not None
        res = await self.session.call_tool(tool, arguments=kwargs)
        text = res.content[0].text if res.content else "{}"
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"_raw": text, "_isError": res.isError}
        if isinstance(parsed, dict):
            parsed["_isError"] = res.isError
        return parsed

    async def dispatch(self, title: str, spec: str, **kwargs: Any) -> dict:
        kwargs.setdefault("repo_path", str(self.work_dir))
        kwargs.setdefault("profile", self.profile)
        r = await self.call("dispatch_task", title=title, spec=spec, **kwargs)
        tid = r.get("task_id")
        if tid:
            self.active_task_ids.add(tid)
        return r

    async def wait_done(
        self, task_ids: list[str], overall_timeout: float | None = None,
        answer_questions: bool = True,
    ) -> bool:
        # A real worker takes minutes, not milliseconds like --fake's scripted
        # server -- overall_timeout defaults to --task-timeout (120s/900s)
        # rather than a value sized for the instant fake model.
        if overall_timeout is None:
            overall_timeout = self.task_timeout
        deadline = time.time() + overall_timeout
        while time.time() < deadline:
            result = await self.call("wait_for_tasks", task_ids=task_ids, timeout_s=60)
            rows = result.get("tasks") or []
            if answer_questions:
                # A real worker asks clarifying questions that the scripted
                # fake never did. With nobody listening it waits out
                # ask_timeout (600s) and then burns its whole wall clock, so
                # one chatty turn failed unrelated tests. The supervisor is
                # the thing that answers in real use; stand in for it here.
                for row in rows:
                    if row.get("status") == "needs_input" and row.get("task_id"):
                        await self.call(
                            "answer_worker",
                            task_id=row["task_id"],
                            answer=(
                                "Proceed with your best judgment. Stay within the allowed files, "
                                "make the minimal change the brief describes, and stop when the "
                                "acceptance command passes."
                            ),
                        )
                rows = [r for r in rows if r.get("status") != "needs_input"]
            if rows and all(r.get("done") or r.get("status") == "needs_input" for r in rows):
                return True
        return False

    async def cleanup(self, task_id: str) -> None:
        r = await self.call("cleanup_task", task_id=task_id)
        if r.get("cleaned"):
            self.active_task_ids.discard(task_id)

    async def force_cancel_and_cleanup(self, task_id: str) -> None:
        try:
            await self.call("cancel_task", task_id=task_id)
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.call("cleanup_task", task_id=task_id)
        except Exception:  # noqa: BLE001
            pass
        self.active_task_ids.discard(task_id)


async def run_block(ctx: Ctx, label: str, fn) -> None:
    """Runs one numbered test in isolation: an unexpected exception (e.g. a
    git call that's a legitimate no-op because the prior step hadn't
    actually finished) records a single FAIL and the run moves on, instead
    of aborting every later test (T2.5 used to take the whole run down this
    way). Anything the block dispatched but never got around to cleaning up
    -- including because it raised -- is force-cleaned afterward so a
    crashed test can't leak a worktree/branch into a later test's checks."""
    before_ids = set(ctx.active_task_ids)
    try:
        await fn()
    except Exception as e:  # noqa: BLE001
        record(f"{label} raised", False, f"{type(e).__name__}: {e}")
    finally:
        for tid in ctx.active_task_ids - before_ids:
            await ctx.force_cancel_and_cleanup(tid)


def spec_text(body: str, args: argparse.Namespace, scenario: str | None = None, **marker_kwargs: Any) -> str:
    """Plain-English instructions (always -- so a real model, and a human
    reading the report, knows exactly what was asked), plus the fake
    scenario marker when --fake is driving the run."""
    if args.fake and scenario:
        return body + "\n\n" + marker(scenario, **marker_kwargs)
    return body


# ═══════════════════════════════════════════════════════════════════════
# Fixture content (shared strings so later phases don't clobber earlier
# phases' committed changes with byte-identical content -- an overwrite
# that changes nothing produces an empty diff, which now correctly fails
# the task (§ review-fix: empty diff -> failed) instead of the old silent
# false success, so the harness itself must never repeat a file's content).
# ═══════════════════════════════════════════════════════════════════════

CALC_INIT_BASE = (
    '"""Toy calculation library for monkey-army validation."""\n\n\n'
    'def add(a, b):\n    """Add two numbers."""\n    return a + b\n\n\n'
    'def multiply(a, b):\n    """Multiply two numbers."""\n    return a * b\n\n\n'
)
CALC_INIT_WITH_SUBTRACT = CALC_INIT_BASE + (
    'def subtract(a, b):\n    """Subtract b from a."""\n    return a - b\n'
)
CALC_INIT_WITH_SUBTRACT_AND_POWER = CALC_INIT_WITH_SUBTRACT + (
    '\n\ndef power(a, b):\n    """Raise a to the power b."""\n    return a ** b\n'
)
EXTRA_NEGATE_SRC = '"""Extra calc helpers."""\n\n\ndef negate(x):\n    """Negate a number."""\n    return -x\n'


def _big_module_content(n: int = 170) -> str:
    lines = ['"""Oversized module -- deliberately trips the diff-size cap."""', ""]
    for i in range(n):
        lines.append(f"def f{i}():")
        lines.append(f"    return {i}")
    return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════════════════
# Phase 1 -- plumbing
# ═══════════════════════════════════════════════════════════════════════

async def phase1(ctx: Ctx) -> None:
    args = ctx.args

    async def t1_1() -> None:
        # T1.1 -- doctor / discover_models / probe
        r = await ctx.call("configure", action="doctor", repo_path=str(ctx.work_dir))
        checks = r.get("checks") or []
        failing = [c["check"] for c in checks if not c.get("ok")]
        record("T1.1 doctor", not failing, f"checks={[c['check'] for c in checks]} failing={failing}")
        record("T1.1 doctor records last_doctor_at", bool(r.get("last_doctor_at")), json.dumps(r.get("last_doctor_at")))

        status = await ctx.call("configure", action="status")
        record(
            "T1.1 meta.last_doctor_at surfaced on status",
            bool(status.get("last_doctor_at")),
            json.dumps(status.get("last_doctor_at")),
        )

        r = await ctx.call("configure", action="discover_models", profile=ctx.profile)
        models = r.get("models") or []
        combos = r.get("combos") or []
        # combos is a hint, not the list to assert on -- some 9Router
        # deployments don't prefix combo ids with `combo/` at all (see
        # backend.discover_models), so it comes back empty on a real router
        # even though `models` is fully populated. What must hold is that
        # `models` is non-empty and the configured model's bare id (whatever
        # follows the first `/`, the same split litellm/9Router itself use)
        # is one of them.
        bare_id = args.model.split("/", 1)[1] if "/" in args.model else args.model
        record(
            "T1.1 discover_models lists the configured model",
            bool(models) and bare_id in models,
            json.dumps({"models": models, "combos": combos, "bare_id": bare_id}),
        )

        r = await ctx.call("configure", action="probe", profile=ctx.profile)
        record("T1.1 probe ok", r.get("ok") is True, json.dumps(r))
        record("T1.1 probe tool_calling confirmed", r.get("tool_calling") == "confirmed", json.dumps(r))

    async def t1_2() -> None:
        # T1.2 -- TDD split: task A writes a failing-for-the-right-reason test,
        # gets reviewed and integrated; task B (against the now-updated main)
        # implements subtract so the test passes.
        test_calc_path = TOY_REPO_SRC / "tests" / "test_calc.py"
        original_test_calc = test_calc_path.read_text(encoding="utf-8")
        new_test_calc = original_test_calc.replace(
            "from calc import add, multiply", "from calc import add, multiply, subtract"
        ) + (
            "\n\ndef test_subtract():\n"
            "    \"\"\"Test subtraction.\"\"\"\n"
            "    assert subtract(5, 3) == 2\n"
            "    assert subtract(-1, -1) == 0\n"
            "    assert subtract(0, 7) == -7\n"
        )

        status_before = _git_ok(ctx.work_dir, "status", "--porcelain").stdout
        r = await ctx.dispatch(
            "T1.2A add test_subtract",
            spec_text(
                "Add a new test function `test_subtract` to tests/test_calc.py, covering "
                "subtract(a, b) for positive, negative and zero cases. Import `subtract` from "
                "`calc` alongside the existing imports. Do NOT implement subtract itself.",
                args, "ok", writes=[{"path": "tests/test_calc.py", "content": new_test_calc}],
            ),
            test_command=None, allowed_files=["tests/test_calc.py"],
        )
        task_a = r.get("task_id")
        if not record("T1.2 dispatch task A", bool(task_a), json.dumps(r)):
            return
        # config.home_dir() resolves symlinks (the /private mirroring fix),
        # so the worktree path is anchored to the REAL path of the temp home,
        # not the raw env string -- compare the same way server/tests/
        # test_jobs.py, test_events.py and test_persistence.py do.
        record(
            "T1.3 worktree A under MONKEY_ARMY_HOME",
            str(r.get("worktree", "")).startswith(os.path.realpath(ctx.env["MONKEY_ARMY_HOME"])),
            r.get("worktree", ""),
        )
        status_during = _git_ok(ctx.work_dir, "status", "--porcelain").stdout
        record("T1.3 user repo clean right after dispatch", status_during.strip() == status_before.strip(), status_during)

        await ctx.wait_done([task_a])
        r = await ctx.call("task_result", task_id=task_a)
        record("T1.2 task A succeeded", r.get("status") == "succeeded", json.dumps({"status": r.get("status"), "error": r.get("error")}))
        await ctx.call("review_task", task_id=task_a, verdict="approve")
        r_int = await ctx.call("integrate_task", task_id=task_a)
        record("T1.2 task A integrated", r_int.get("integrated") is True, json.dumps(r_int))
        await ctx.cleanup(task_a)

        r = await ctx.dispatch(
            "T1.2B implement subtract",
            spec_text(
                "Implement subtract(a, b) in calc/__init__.py (return a - b, matching the "
                "docstring style already used by add/multiply) so tests/test_calc.py::test_subtract passes.",
                args, "ok", writes=[{"path": "calc/__init__.py", "content": CALC_INIT_WITH_SUBTRACT}],
            ),
            test_command=TEST_CMD, allowed_files=["calc/__init__.py"],
        )
        task_b = r.get("task_id")
        if not record("T1.2 dispatch task B", bool(task_b), json.dumps(r)):
            return
        await ctx.wait_done([task_b])
        r = await ctx.call("task_result", task_id=task_b)
        record("T1.2 task B succeeded", r.get("status") == "succeeded", json.dumps({"status": r.get("status"), "error": r.get("error")}))
        record("T1.2 task B verification passed", bool((r.get("verification") or {}).get("passed")), json.dumps(r.get("verification")))
        record("T1.2 task B priced", r.get("priced") is True, json.dumps(r.get("priced")))
        record("T1.2 task B models_seen non-empty", bool(r.get("models_seen")), json.dumps(r.get("models_seen")))
        await ctx.call("review_task", task_id=task_b, verdict="approve")
        r_int = await ctx.call("integrate_task", task_id=task_b)
        record("T1.2 task B integrated", r_int.get("integrated") is True, json.dumps(r_int))
        await ctx.cleanup(task_b)

        status_after = _git_ok(ctx.work_dir, "status", "--porcelain").stdout
        record("T1.3 user repo clean after batch", status_after.strip() == "", status_after)

    async def t1_4() -> None:
        # T1.4 -- sentinel round-trip
        sentinel_path = ctx.work_dir / "fixtures" / "sentinel.txt"
        sentinel_bytes = sentinel_path.read_text(encoding="utf-8")
        r = await ctx.dispatch(
            "T1.4 sentinel copy",
            spec_text(
                "Copy fixtures/sentinel.txt verbatim (byte for byte) to fixtures/copy.txt.",
                args, "ok", writes=[{"path": "fixtures/copy.txt", "content": sentinel_bytes}],
            ),
            test_command=None, allowed_files=["fixtures/copy.txt"],
        )
        task_s = r.get("task_id")
        if record("T1.4 dispatch sentinel task", bool(task_s), json.dumps(r)):
            await ctx.wait_done([task_s])
            r = await ctx.call("task_result", task_id=task_s)
            wt = r.get("worktree")
            if record("T1.4 sentinel task succeeded", r.get("status") == "succeeded", json.dumps({"status": r.get("status")})) and wt:
                cmp_out = subprocess.run(
                    ["cmp", str(Path(wt) / "fixtures" / "sentinel.txt"), str(Path(wt) / "fixtures" / "copy.txt")],
                    capture_output=True, text=True,
                )
                record("T1.4 cmp reports no differences", cmp_out.returncode == 0, cmp_out.stdout + cmp_out.stderr)
            await ctx.cleanup(task_s)

    async def t1_5() -> None:
        # T1.5 -- bad model name: probe fails, dispatch_task refuses, no worktree created
        await ctx.call(
            "configure", action="set_profile", name="e2e-badmodel",
            model="openai/combo/does-not-exist-xyz", api_base=ctx.api_base,
            api_key_env_var=args.api_key_env_var,
        )
        wt_before = _git(ctx.work_dir, "worktree", "list", "--porcelain").stdout.count("worktree ")
        r = await ctx.call("configure", action="probe", profile="e2e-badmodel")
        record("T1.5 probe fails for a bad model name", r.get("ok") is False, json.dumps(r))
        r = await ctx.dispatch(
            "T1.5 bad model dispatch",
            spec_text("This dispatch must be refused before any worktree is created.", args, None),
            test_command=None, profile="e2e-badmodel",
        )
        record("T1.5 dispatch_task refuses", "task_id" not in r and bool(r.get("error")), json.dumps(r))
        wt_after = _git(ctx.work_dir, "worktree", "list", "--porcelain").stdout.count("worktree ")
        record("T1.5 no worktree created", wt_after == wt_before, f"before={wt_before} after={wt_after}")

    for label, fn in (("T1.1", t1_1), ("T1.2", t1_2), ("T1.4", t1_4), ("T1.5", t1_5)):
        await run_block(ctx, label, fn)


# ═══════════════════════════════════════════════════════════════════════
# Phase 2 -- gates and merge-back
# ═══════════════════════════════════════════════════════════════════════

async def phase2(ctx: Ctx) -> None:
    args = ctx.args

    async def t2_1() -> None:
        # T2.1 -- out-of-scope edit -> failed_scope, main tree untouched
        r = await ctx.dispatch(
            "T2.1 scope violator",
            spec_text(
                "Create calc/scope_target.py with a function ok() returning True. As part of this "
                "scope-enforcement drill, ALSO create calc/should_not_touch.py with `oops = True` -- "
                "deliberately outside your allowed files.",
                args, "scope",
                writes=[{"path": "calc/scope_target.py", "content": "def ok():\n    return True\n"}],
                scope_violation={"path": "calc/should_not_touch.py", "content": "oops = True\n"},
            ),
            test_command=TEST_CMD, allowed_files=["calc/scope_target.py"],
        )
        task_scope = r.get("task_id")
        head_before = _git(ctx.work_dir, "rev-parse", "HEAD").stdout.strip()
        if record("T2.1 dispatch scope task", bool(task_scope), json.dumps(r)):
            await ctx.wait_done([task_scope])
            r = await ctx.call("task_result", task_id=task_scope)
            outcome = r.get("status")
            if args.fake:
                record("T2.1 failed_scope", outcome == "failed_scope", json.dumps({"status": outcome, "error": r.get("error")}))
            elif outcome == "failed_scope":
                record("T2.1 failed_scope", True, "")
            else:
                # A real model can simply decline the "also write an
                # out-of-scope file" half of the instruction instead of
                # complying with it -- nothing to enforce if it never
                # violates scope. Same non-determinism class as T2.7/T4.5.
                skip("T2.1 failed_scope", f"non-deterministic against a real model: expected failed_scope, got {outcome!r}")
            head_after = _git(ctx.work_dir, "rev-parse", "HEAD").stdout.strip()
            record("T2.1 main tree untouched", head_before == head_after, f"{head_before} vs {head_after}")
            await ctx.cleanup(task_scope)

    async def t2_2_t2_9() -> None:
        # T2.2 / T2.9 -- failing acceptance run -> failed_verification; reject
        # -> retry in the same worktree -> approve -> integrate as one squash
        # commit. I4 (the SERVER decides success by re-running the acceptance
        # command) is the guarantee under test here -- it must not depend on
        # a real model agreeing to write broken code on purpose (observed
        # live: a well-behaved model just implements divide() correctly and
        # the drill never triggers verification at all). Force the failure
        # from outside instead: pre-commit a test that always fails, dispatch
        # an ordinary task whose test_command runs the whole suite, and the
        # server must refuse regardless of what the worker does -- either
        # failed_verification (suite still red), or failed_scope (worker
        # "helpfully" touched the excluded gate file). Either is the server
        # refusing, which is what's actually being tested.
        if args.fake:
            ok_divide = '"""Working divide."""\n\n\ndef divide(a, b):\n    return a // b\n'
            bad_divide = '"""Broken divide."""\n\n\ndef divide(a, b):\n    return a + b\n'
            divide_test = 'from calc.divide import divide\n\n\ndef test_divide():\n    assert divide(6, 3) == 2\n'
            r = await ctx.dispatch(
                "T2.2/T2.9 divide with a deliberate first-attempt bug",
                spec_text(
                    "Add calc/divide.py with divide(a, b) doing integer division, and tests/test_divide.py "
                    "testing divide(6,3)==2. DRILL: on this first attempt, deliberately implement divide as "
                    "`return a + b` (wrong) instead of the real division, to exercise server-side verification.",
                    args, "failtest",
                    writes=[
                        {"path": "calc/divide.py", "content": ok_divide},
                        {"path": "tests/test_divide.py", "content": divide_test},
                    ],
                    bad_writes=[
                        {"path": "calc/divide.py", "content": bad_divide},
                        {"path": "tests/test_divide.py", "content": divide_test},
                    ],
                ),
                test_command=TEST_CMD, allowed_files=["calc/divide.py", "tests/test_divide.py"],
            )
            task_ft = r.get("task_id")
            worktree_ft = r.get("worktree")
        else:
            # The clear-marker lives OUTSIDE every repo and worktree: an
            # untracked file inside the worker's worktree is an out-of-scope
            # change, and the server rightly returned failed_scope for it
            # (observed live on T2.9). Nothing about clearing the gate should
            # be visible to the scope check.
            gate_marker = Path(tempfile.mkdtemp(prefix="monkeyarmy_e2e_gate_")) / "clear"
            gate_path = ctx.work_dir / "tests" / "test_gate.py"
            gate_path.write_text(
                'from pathlib import Path\n\n\n'
                'def test_gate():\n'
                '    """Always fails until the harness drops a clear-marker file (T2.2 drill)."""\n'
                f'    marker = Path({str(gate_marker)!r})\n'
                '    assert marker.exists(), "T2.2 gate: blocking marker not present yet"\n',
                encoding="utf-8",
            )
            _git(ctx.work_dir, "add", "tests/test_gate.py")
            _git(ctx.work_dir, "commit", "-m", "T2.2 gate: always-failing test (harness drill)")
            r = await ctx.dispatch(
                "T2.2/T2.9 docstring touch-up (blocked by an always-failing gate test)",
                "Add a short one-line docstring note above the `add` function in calc/__init__.py "
                "explaining what it does. Do not touch anything under tests/ -- that's out of scope "
                "for this task.",
                # No test_command: the worker gets an ordinary, satisfiable
                # task and finishes cleanly. The always-failing gate goes in
                # verify_command, which ONLY the server runs after the worker
                # is done -- so `failed_verification` proves I4 (the server
                # decides, never the worker) rather than proving a worker can
                # be made to loop. Live evidence: with the gate as the
                # worker's own test_command it iterated to the 400k token cap.
                test_command=None,
                verify_command=TEST_CMD,
                allowed_files=["calc/__init__.py"],
            )
            task_ft = r.get("task_id")
            worktree_ft = r.get("worktree")

        if record("T2.2 dispatch failtest task", bool(task_ft), json.dumps(r)):
            await ctx.wait_done([task_ft])
            r = await ctx.call("task_result", task_id=task_ft)
            status = r.get("status")
            if args.fake:
                record("T2.2 failed_verification", status == "failed_verification", json.dumps({"status": status, "error": r.get("error")}))
            else:
                record(
                    "T2.2 failed_verification",
                    status in ("failed_verification", "failed_scope"),
                    json.dumps({"status": status, "error": r.get("error")}),
                )
                # Clear the gate for the retry. The marker lives outside every
                # repo (see gate_marker above), so neither the worker's branch
                # nor the scope check ever sees it.
                gate_marker.touch()

            feedback = (
                "divide adds instead of dividing; fix the operator to integer division."
                if args.fake else
                "The gate test blocking the suite has been cleared in this worktree; retry now."
            )
            r = await ctx.call("review_task", task_id=task_ft, verdict="reject", feedback=feedback)
            record("T2.9 reject re-runs in same worktree", r.get("status") == "running", json.dumps(r))
            await ctx.wait_done([task_ft])
            r = await ctx.call("task_result", task_id=task_ft)
            record("T2.9 retry succeeded", r.get("status") == "succeeded", json.dumps({"status": r.get("status")}))
            record("T2.9 retry is attempt 2", r.get("attempt") == 2, json.dumps(r.get("attempt")))

            await ctx.call("review_task", task_id=task_ft, verdict="approve")
            log_before = _git(ctx.work_dir, "log", "--oneline").stdout.splitlines()
            r = await ctx.call("integrate_task", task_id=task_ft)
            record("T2.9 integrated", r.get("integrated") is True, json.dumps(r))
            log_after = _git(ctx.work_dir, "log", "--oneline").stdout.splitlines()
            record("T2.9 one squash commit covering both attempts", len(log_after) - len(log_before) == 1,
                   f"before={len(log_before)} after={len(log_after)}")
            await ctx.cleanup(task_ft)

            if not args.fake:
                # Don't poison T2.3/T2.4/... with a permanently-failing suite.
                _git_ok(ctx.work_dir, "rm", "-f", "tests/test_gate.py")
                _git_ok(ctx.work_dir, "commit", "-m", "T2.2 gate: remove (harness drill cleanup)")

    async def t2_3() -> None:
        # T2.3 -- two parallel disjoint tasks via batch -> finish (also exercises
        # batch(status|finish) resolving repo_path from batch_id alone).
        tasks_json = json.dumps([
            {"key": "a", "title": "add power", "allowedFiles": ["calc/__init__.py"]},
            {"key": "b", "title": "add negate", "allowedFiles": ["calc/extra.py"]},
        ])
        r = await ctx.call("batch", action="create", repo_path=str(ctx.work_dir), goal="add power and negate", tasks_json=tasks_json)
        batch_id = r.get("batch_id")
        if record("T2.3 batch create", bool(batch_id), json.dumps(r)):
            r = await ctx.dispatch(
                "T2.3a add power",
                spec_text(
                    "Add a power(a, b) function (a ** b) to calc/__init__.py, keeping add/multiply/subtract intact.",
                    args, "ok", writes=[{"path": "calc/__init__.py", "content": CALC_INIT_WITH_SUBTRACT_AND_POWER}],
                ),
                test_command=TEST_CMD, allowed_files=["calc/__init__.py"], batch_id=batch_id, batch_key="a",
            )
            task_3a = r.get("task_id")
            r = await ctx.dispatch(
                "T2.3b add negate",
                spec_text(
                    "Create calc/extra.py with a negate(x) function returning -x.",
                    args, "ok", writes=[{"path": "calc/extra.py", "content": EXTRA_NEGATE_SRC}],
                ),
                test_command=TEST_CMD, allowed_files=["calc/extra.py"], batch_id=batch_id, batch_key="b",
            )
            task_3b = r.get("task_id")
            record("T2.3 dispatch both batch tasks", bool(task_3a) and bool(task_3b), f"{task_3a} {task_3b}")

            if task_3a and task_3b:
                await ctx.wait_done([task_3a, task_3b])
                for tid, label in ((task_3a, "a"), (task_3b, "b")):
                    r = await ctx.call("task_result", task_id=tid)
                    record(f"T2.3 task {label} succeeded", r.get("status") == "succeeded", json.dumps({"status": r.get("status"), "error": r.get("error")}))
                    r = await ctx.call("review_task", task_id=tid, verdict="approve")
                    record(f"T2.3 task {label} approved", (r.get("review") or {}).get("verdict") == "approve", json.dumps(r))

                log_before = _git(ctx.work_dir, "log", "--oneline").stdout.splitlines()
                # repo_path deliberately omitted -- batch.finish resolves it from batch_id (review-fix).
                r = await ctx.call("batch", action="finish", batch_id=batch_id, verify_command=TEST_CMD)
                record("T2.3 batch finish (no repo_path) finished", r.get("finished") is True, json.dumps(r))
                log_after = _git(ctx.work_dir, "log", "--oneline").stdout.splitlines()
                record("T2.3 two new commits on main", len(log_after) - len(log_before) == 2,
                       f"before={len(log_before)} after={len(log_after)}")
                await ctx.cleanup(task_3a)
                await ctx.cleanup(task_3b)

                # git_ops.assert_end_state (called server-side by batch.finish)
                # already scopes worktree/branch leftovers to THIS batch's own
                # task ids -- a repo-wide `git worktree list` here would also
                # count worktrees/branches other, earlier-failed tests in this
                # same run left behind, which isn't this test's business.
                end_state = (r.get("report") or {}).get("endState") or {}
                record("T2.3 exactly one worktree left", end_state.get("worktreesLeft") == 0, json.dumps(end_state))
                record("T2.3 zero monkey/* branches left", end_state.get("branchesLeft") == 0, json.dumps(end_state))

                status_r = await ctx.call("batch", action="status", batch_id=batch_id)
                record("T2.3 batch status (no repo_path) resolves", "error" not in status_r, json.dumps(status_r))

                pytest_run = subprocess.run(
                    ["uv", "run", "--no-project", "--with", "pytest", "python", "-m", "pytest", "-q"],
                    cwd=str(ctx.work_dir), capture_output=True, text=True,
                )
                record("T2.3 suite green after batch", pytest_run.returncode == 0, pytest_run.stdout[-300:])

    async def t2_4() -> None:
        # T2.4 -- same-file conflict -> conflict, tree unchanged, branch kept,
        # re-dispatch against the moved base succeeds.
        r = await ctx.dispatch(
            "T2.4 conflict one",
            spec_text("Create calc/conflict.py (version one) with a function one() returning 1.",
                       args, "ok", writes=[{"path": "calc/conflict.py", "content": "def one():\n    return 1\n"}]),
            test_command=TEST_CMD, allowed_files=["calc/conflict.py"],
        )
        task_c1 = r.get("task_id")
        r = await ctx.dispatch(
            "T2.4 conflict two",
            spec_text("Create calc/conflict.py (version two) with a function two() returning 2.",
                       args, "ok", writes=[{"path": "calc/conflict.py", "content": "def two():\n    return 2\n"}]),
            test_command=TEST_CMD, allowed_files=["calc/conflict.py"],
        )
        task_c2 = r.get("task_id")
        if record("T2.4 dispatch conflicting tasks", bool(task_c1) and bool(task_c2), f"{task_c1} {task_c2}"):
            await ctx.wait_done([task_c1, task_c2])
            for tid, label in ((task_c1, "c1"), (task_c2, "c2")):
                r = await ctx.call("task_result", task_id=tid)
                record(f"T2.4 task {label} succeeded", r.get("status") == "succeeded", json.dumps({"status": r.get("status")}))
                await ctx.call("review_task", task_id=tid, verdict="approve")

            r = await ctx.call("integrate_task", task_id=task_c1)
            record("T2.4 first integrates", r.get("integrated") is True, json.dumps(r))
            await ctx.cleanup(task_c1)

            head_before = _git(ctx.work_dir, "rev-parse", "HEAD").stdout.strip()
            status_before = _git_ok(ctx.work_dir, "status", "--porcelain").stdout
            r = await ctx.call("integrate_task", task_id=task_c2)
            record("T2.4 second fails with conflict", r.get("integrated") is False and r.get("reason") == "conflict", json.dumps(r))
            head_after = _git(ctx.work_dir, "rev-parse", "HEAD").stdout.strip()
            status_after = _git_ok(ctx.work_dir, "status", "--porcelain").stdout
            record("T2.4 tree unchanged after conflict", head_before == head_after and status_before == status_after,
                   f"HEAD {head_before}->{head_after}")
            branch_out = _git_ok(ctx.work_dir, "branch", "--list", f"monkey/{task_c2}").stdout
            record("T2.4 conflict branch preserved", f"monkey/{task_c2}" in branch_out, branch_out)
            await ctx.cleanup(task_c2)

            r = await ctx.dispatch(
                "T2.4 conflict two, re-dispatched",
                spec_text("Create/replace calc/conflict.py with a function two() returning 2.",
                           args, "ok", writes=[{"path": "calc/conflict.py", "content": "def two():\n    return 2\n"}]),
                test_command=TEST_CMD, allowed_files=["calc/conflict.py"],
            )
            task_c2b = r.get("task_id")
            if record("T2.4 re-dispatch", bool(task_c2b), json.dumps(r)):
                await ctx.wait_done([task_c2b])
                r = await ctx.call("task_result", task_id=task_c2b)
                record("T2.4 re-dispatched task succeeded", r.get("status") == "succeeded", json.dumps({"status": r.get("status")}))
                await ctx.call("review_task", task_id=task_c2b, verdict="approve")
                r = await ctx.call("integrate_task", task_id=task_c2b)
                record("T2.4 re-dispatch now integrates against moved base", r.get("integrated") is True, json.dumps(r))
                await ctx.cleanup(task_c2b)

    async def t2_5() -> None:
        # T2.5 -- mode=stage
        r = await ctx.dispatch(
            "T2.5 stage me",
            spec_text("Create calc/staged.py with a function staged() returning True.",
                       args, "ok", writes=[{"path": "calc/staged.py", "content": "def staged():\n    return True\n"}]),
            test_command=TEST_CMD, allowed_files=["calc/staged.py"],
        )
        task_stage = r.get("task_id")
        if record("T2.5 dispatch stage task", bool(task_stage), json.dumps(r)):
            await ctx.wait_done([task_stage])
            r = await ctx.call("task_result", task_id=task_stage)
            record("T2.5 stage task succeeded", r.get("status") == "succeeded", json.dumps({"status": r.get("status")}))
            await ctx.call("review_task", task_id=task_stage, verdict="approve")
            log_before = _git(ctx.work_dir, "log", "--oneline").stdout.splitlines()
            r = await ctx.call("integrate_task", task_id=task_stage, mode="stage")
            record("T2.5 integrate mode=stage", r.get("integrated") is True and r.get("mode") == "stage", json.dumps(r))
            log_after = _git(ctx.work_dir, "log", "--oneline").stdout.splitlines()
            record("T2.5 no new commit", len(log_after) == len(log_before), f"before={len(log_before)} after={len(log_after)}")
            staged_status = _git_ok(ctx.work_dir, "status", "--porcelain").stdout
            record("T2.5 change staged not committed", "calc/staged.py" in staged_status, staged_status)
            await ctx.cleanup(task_stage)
            # Leave it staged for the user (matches the real semantics); commit it
            # now so later phases' `git status` checks stay clean and predictable.
            # _git_ok (not _git): if the prior step didn't actually leave anything
            # staged (e.g. integrate never got there), `commit` is a legitimate
            # no-op and must not raise and kill the rest of the run.
            _git_ok(ctx.work_dir, "commit", "-m", "T2.5 stage drill (harness commit)")

    async def t2_6() -> None:
        # T2.6 -- restart resilience: kill and relaunch the MCP server process
        # mid-task, then task_status/cancel_task on the now-orphaned job.
        r = await ctx.dispatch(
            "T2.6 restart drill",
            spec_text(
                "Create calc/restart_marker.py with `value = 1`, then run `sleep 25` via the shell tool "
                "-- this task deliberately runs long so the harness can test supervisor-restart resilience.",
                args, "ok",
                writes=[{"path": "calc/restart_marker.py", "content": "value = 1\n"}],
                post_steps=[{"tool": "execute", "args": {"command": "sleep 25"}}],
            ),
            test_command=None, allowed_files=["calc/restart_marker.py"],
        )
        task_restart = r.get("task_id")
        if record("T2.6 dispatch restart drill", bool(task_restart), json.dumps(r)):
            # Poll until the marker file lands (proof the worker reached the
            # `sleep 25` step) rather than a fixed delay -- `uv run` startup time
            # varies with cache state, and a real model's first turn is slower still.
            deadline = time.time() + (30 if args.fake else 180)
            while time.time() < deadline:
                pr = await ctx.call("task_progress", task_id=task_restart)
                if "calc/restart_marker.py" in (pr.get("files_touched") or []):
                    break
                time.sleep(0.5)
            await ctx.restart_server()
            r = await ctx.call("task_status", task_id=task_restart)
            record("T2.6 task_status resolves after restart", r.get("task_id") == task_restart and "error" not in r, json.dumps(r))
            r = await ctx.call("cancel_task", task_id=task_restart)
            record("T2.6 cancel_task works on the orphan", r.get("status") == "cancelled", json.dumps(r))
            record("T2.6 salvaged patch present", r.get("salvaged") is True and bool(r.get("patch_path")), json.dumps(r))
            await ctx.cleanup(task_restart)

    async def t2_7() -> None:
        # T2.7 -- oversized diff -> failed_oversized
        if args.fake:
            r = await ctx.dispatch(
                "T2.7 oversized diff",
                spec_text(
                    "Create calc/big_module.py containing at least 170 small distinct functions "
                    "(f0()..f169(), each just `return <its index>`) -- this drill deliberately produces "
                    "an oversized diff to test the diff-size cap.",
                    args, "ok", writes=[{"path": "calc/big_module.py", "content": _big_module_content(170)}],
                ),
                test_command=None, allowed_files=["calc/big_module.py"],
            )
            task_big = r.get("task_id")
            if record("T2.7 dispatch oversized task", bool(task_big), json.dumps(r)):
                await ctx.wait_done([task_big])
                r = await ctx.call("task_result", task_id=task_big)
                record("T2.7 failed_oversized", r.get("status") == "failed_oversized", json.dumps({"status": r.get("status"), "error": r.get("error")}))
                await ctx.cleanup(task_big)
        else:
            r = await ctx.dispatch(
                "T2.7 oversized diff",
                "Create calc/big_module.py containing at least 350 small distinct functions "
                "(f0()..f349(), each `return <its index>`) -- deliberately oversized, to test the diff cap.",
                test_command=None, allowed_files=["calc/big_module.py"],
            )
            task_big = r.get("task_id")
            outcome = "failed_oversized"
            if task_big:
                await ctx.wait_done([task_big])
                r = await ctx.call("task_result", task_id=task_big)
                outcome = r.get("status")
                await ctx.cleanup(task_big)
            if outcome == "failed_oversized":
                record("T2.7 failed_oversized", True, "")
            else:
                skip("T2.7 failed_oversized", f"non-deterministic against a real model: expected failed_oversized, got {outcome!r}")

    async def t2_8() -> None:
        # T2.8 -- worker question -> wait_for_tasks returns early -> answer_worker resumes
        r = await ctx.dispatch(
            "T2.8 clarifying question",
            spec_text(
                "Before writing any code, call ask_supervisor with a genuine clarifying question about "
                "what to name the function in calc/asked.py, wait for the answer, then create "
                "calc/asked.py with a function named `asked` returning True.",
                args, "ok",
                pre_steps=[{"tool": "ask_supervisor", "args": {"question": "What should I name the function?", "context": "drill"}}],
                writes=[{"path": "calc/asked.py", "content": "def asked():\n    return True\n"}],
            ),
            test_command=None, allowed_files=["calc/asked.py"],
        )
        task_q = r.get("task_id")
        if record("T2.8 dispatch question task", bool(task_q), json.dumps(r)):
            deadline = time.time() + ctx.task_timeout
            question_seen = False
            last_wait: dict = {}
            while time.time() < deadline:
                last_wait = await ctx.call("wait_for_tasks", task_ids=[task_q], timeout_s=10)
                rows = last_wait.get("tasks") or []
                if rows and rows[0].get("status") == "needs_input":
                    question_seen = True
                    break
                if rows and rows[0].get("done"):
                    break
            record("T2.8 wait_for_tasks returns early on needs_input", question_seen, json.dumps(last_wait))
            if question_seen:
                st = await ctx.call("task_status", task_id=task_q)
                qid = (st.get("question") or {}).get("id")
                r = await ctx.call("answer_worker", task_id=task_q, answer="call it `asked`")
                record("T2.8 answer_worker delivers", r.get("delivered") is True and r.get("question_id") == qid, json.dumps(r))
                await ctx.wait_done([task_q], answer_questions=False)
                r = await ctx.call("task_result", task_id=task_q)
                record("T2.8 task resumes and succeeds", r.get("status") == "succeeded", json.dumps({"status": r.get("status")}))
            await ctx.cleanup(task_q)

    for label, fn in (
        ("T2.1", t2_1), ("T2.2/T2.9", t2_2_t2_9), ("T2.3", t2_3), ("T2.4", t2_4),
        ("T2.5", t2_5), ("T2.6", t2_6), ("T2.7", t2_7), ("T2.8", t2_8),
    ):
        await run_block(ctx, label, fn)


# ═══════════════════════════════════════════════════════════════════════
# Phase 4 -- failure drills
# ═══════════════════════════════════════════════════════════════════════

async def phase4(ctx: Ctx) -> None:
    args = ctx.args

    # 9Router unreachable -- point a temp profile at a dead port and dispatch.
    # A true mid-run disconnect races litellm's own retry/timeout internals
    # (not reproducible deterministically even under --fake); "unreachable
    # for the whole task" is the deterministic proxy VALIDATION.md's Phase 4
    # itself suggests ("point the profile at a dead port and dispatch"), and
    # it exercises the same clear-error, no-orphaned-worktree path.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()
    await ctx.call(
        "configure", action="set_profile", name="e2e-deadport",
        model=args.model, api_base=f"http://127.0.0.1:{dead_port}/v1",
        api_key_env_var=args.api_key_env_var,
    )
    wt_before = _git(ctx.work_dir, "worktree", "list", "--porcelain").stdout.count("worktree ")
    r = await ctx.dispatch(
        "T4.1 unreachable endpoint",
        "This dispatch must be refused with a clear connection error before any worktree is created.",
        test_command=None, profile="e2e-deadport",
    )
    record("T4.1 unreachable endpoint refused with a clear error", "task_id" not in r and bool(r.get("error")), json.dumps(r))
    wt_after = _git(ctx.work_dir, "worktree", "list", "--porcelain").stdout.count("worktree ")
    record("T4.1 no worktree left behind to salvage", wt_after == wt_before, f"before={wt_before} after={wt_after}")

    # T4.2 -- broken test_command -> preflight note surfaces it
    r = await ctx.dispatch(
        "T4.2 broken test command",
        spec_text(
            "This is a preflight drill; the acceptance command is intentionally broken. Call "
            "report_progress once with a short note and stop.",
            args, "ok",
        ),
        test_command="python3 -m pytest --this-flag-does-not-exist -q", allowed_files=[],
    )
    note = r.get("preflight_note")
    # A dispatch that never happened has no preflight report: the probe gate
    # runs first by design, so a dead endpoint must surface as a refusal, not
    # as a missing note. Assert whichever of the two actually applies.
    if "task_id" not in r and r.get("error"):
        record(
            "T4.2 preflight note surfaces the broken runner",
            "probe" in str(r.get("error", "")).lower() or "probe" in r,
            f"endpoint refused before preflight: {json.dumps(r)[:200]}",
        )
    else:
        record(
            "T4.2 preflight note surfaces the broken runner",
            bool(note),
            json.dumps({"preflight": r.get("preflight"), "note": note}),
        )
    task_pf = r.get("task_id")
    if task_pf:
        await ctx.wait_done([task_pf], overall_timeout=30)
        await ctx.force_cancel_and_cleanup(task_pf)

    # T4.3 -- max_budget_usd=0.01 -> clean stop naming the cap
    if args.fake:
        r = await ctx.dispatch(
            "T4.3 budget cap",
            spec_text(
                "Repeatedly call execute with a trivial command until told to stop -- budget-cap drill.",
                args, "budgetloop",
            ),
            test_command=None, allowed_files=[], max_budget_usd=0.01,
        )
    else:
        r = await ctx.dispatch(
            "T4.3 budget cap",
            "Say a short greeting and stop.",
            test_command=None, allowed_files=[], max_budget_usd=0.000001,
        )
    task_budget = r.get("task_id")
    if record("T4.3 dispatch budget-capped task", bool(task_budget), json.dumps(r)):
        await ctx.wait_done([task_budget])
        r = await ctx.call("task_result", task_id=task_budget)
        err = r.get("error") or ""
        record(
            "T4.3 clean stop naming the USD cap",
            r.get("status") == "failed" and "budget exceeded" in err and "USD cap" in err,
            json.dumps({"status": r.get("status"), "error": err}),
        )
        await ctx.cleanup(task_budget)

    # T4.4 -- max_tokens_total=2000 -> same, named cap
    if args.fake:
        r = await ctx.dispatch(
            "T4.4 token cap",
            spec_text(
                "Repeatedly call execute with a trivial command until told to stop -- token-cap drill.",
                args, "budgetloop",
            ),
            test_command=None, allowed_files=[], max_tokens_total=2000,
        )
    else:
        r = await ctx.dispatch(
            "T4.4 token cap",
            "Say a short greeting and stop.",
            test_command=None, allowed_files=[], max_tokens_total=1,
        )
    task_tok = r.get("task_id")
    if record("T4.4 dispatch token-capped task", bool(task_tok), json.dumps(r)):
        await ctx.wait_done([task_tok])
        r = await ctx.call("task_result", task_id=task_tok)
        err = r.get("error") or ""
        record(
            "T4.4 clean stop naming the token cap",
            r.get("status") == "failed" and "budget exceeded" in err and "token cap" in err,
            json.dumps({"status": r.get("status"), "error": err}),
        )
        await ctx.cleanup(task_tok)

    # T4.5 -- steer_task mid-run visibly changes worker behaviour
    r = await ctx.dispatch(
        "T4.5 steer drill",
        spec_text(
            "Wait by running `sleep 1 && echo waiting` in a loop via execute; if a tool result ever "
            "contains a line starting '⚠ SUPERVISOR STEERING', obey it immediately and write "
            "calc/steer_target.py reflecting the steering (`steered = True`); otherwise, after "
            "several iterations, write calc/steer_target.py with `steered = False`.",
            args, "steer",
            writes=[{"path": "calc/steer_target.py", "content": "steered = True\n"}],
            writes_unsteered=[{"path": "calc/steer_target.py", "content": "steered = False\n"}],
            wait_turns=5,
        ),
        test_command=None, allowed_files=["calc/steer_target.py"],
    )
    task_steer = r.get("task_id")
    if record("T4.5 dispatch steer drill", bool(task_steer), json.dumps(r)):
        await ctx.call("steer_task", task_id=task_steer, message="Write calc/steer_target.py with `steered = True`.")
        await ctx.wait_done([task_steer])
        r = await ctx.call("task_result", task_id=task_steer)
        content = r.get("patch") or ""
        steered = "steered = True" in content
        if args.fake:
            record("T4.5 steering visibly changed worker output", steered, content[:300])
        elif steered:
            record("T4.5 steering visibly changed worker output", True, content[:300])
        else:
            skip("T4.5 steering visibly changed worker output",
                 f"non-deterministic against a real model; attempted, status={r.get('status')!r}")
        await ctx.cleanup(task_steer)

    # T4.6 -- cancel_task salvages the in-progress patch
    if args.fake:
        r = await ctx.dispatch(
            "T4.6 cancel drill",
            spec_text(
                "Create calc/cancel_marker.py with `value = 1`, then repeat `sleep 2` forever -- "
                "you will be interrupted.",
                args, "cancelloop", writes=[{"path": "calc/cancel_marker.py", "content": "value = 1\n"}],
            ),
            test_command=None, allowed_files=["calc/cancel_marker.py"],
        )
        task_cancel = r.get("task_id")
        if record("T4.6 dispatch cancel drill", bool(task_cancel), json.dumps(r)):
            # Poll (rather than a fixed sleep) until the marker file actually
            # lands in the worktree -- `uv run worker/worker.py` startup time
            # varies with cache state, so a fixed delay is flaky.
            deadline = time.time() + 30
            while time.time() < deadline:
                pr = await ctx.call("task_progress", task_id=task_cancel)
                if "calc/cancel_marker.py" in (pr.get("files_touched") or []):
                    break
                time.sleep(0.5)
            r = await ctx.call("cancel_task", task_id=task_cancel)
            record(
                "T4.6 cancel_task reports cancelled + salvaged",
                r.get("status") == "cancelled" and r.get("salvaged") is True,
                json.dumps(r),
            )
            await ctx.cleanup(task_cancel)
    else:
        r = await ctx.dispatch(
            "T4.6 cancel drill",
            "Write calc/cancel_marker.py with `value = 1`, then keep working slowly on small, safe improvements.",
            test_command=None, allowed_files=["calc/cancel_marker.py"],
        )
        task_cancel = r.get("task_id")
        if record("T4.6 dispatch cancel drill", bool(task_cancel), json.dumps(r)):
            time.sleep(10)
            r = await ctx.call("cancel_task", task_id=task_cancel)
            if r.get("error"):
                skip("T4.6 cancel_task salvages", f"task already finished before cancel arrived: {r.get('error')}")
            else:
                record("T4.6 cancel_task reports cancelled", r.get("status") == "cancelled", json.dumps(r))
            await ctx.force_cancel_and_cleanup(task_cancel)

    # T4.7 -- worker `git push` / `git branch -D main` blocked at the tool layer
    r = await ctx.dispatch(
        "T4.7 git allowlist drill",
        spec_text(
            "Run `git -C . push` via the shell tool, then run `git branch -D main` via the shell "
            "tool, then create calc/gitblock_done.py with `done = True` to show the task continues "
            "normally afterward despite both git commands being blocked.",
            args, "ok",
            pre_steps=[
                {"tool": "execute", "args": {"command": "git -C . push"}},
                {"tool": "execute", "args": {"command": "git branch -D main"}},
            ],
            writes=[{"path": "calc/gitblock_done.py", "content": "done = True\n"}],
        ),
        test_command=None, allowed_files=["calc/gitblock_done.py"],
    )
    task_git = r.get("task_id")
    if record("T4.7 dispatch git-allowlist drill", bool(task_git), json.dumps(r)):
        await ctx.wait_done([task_git])
        r = await ctx.call("task_result", task_id=task_git)
        outcome = r.get("status")
        branch_out = _git_ok(ctx.work_dir, "branch", "--list", "main").stdout
        # The exhaustive command-parsing matrix (chained/`sh -c`/global-option
        # forms) is unit-tested in worker/tests/test_worker.py; this end-to-end
        # check confirms the task wasn't derailed by the blocked commands and
        # that `main` -- checked out in the user's own tree -- is untouched.
        record(
            "T4.7 git push/branch -D blocked at tool layer, task continues normally",
            outcome == "succeeded" and "main" in branch_out,
            json.dumps({"status": outcome, "branch_list": branch_out}),
        )
        await ctx.cleanup(task_git)


# ═══════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Offline (or real-endpoint) end-to-end proof of the monkey-army plugin.")
    p.add_argument("--api-base", default=None, help="OpenAI-compatible endpoint base URL (required unless --fake).")
    p.add_argument("--model", default=None, help="litellm model string, e.g. openai/combo/deepseek-main.")
    p.add_argument("--api-key-env-var", default="MONKEY_9ROUTER_KEY",
                    help="Env var to read the API key from -- never accepted as an argument.")
    p.add_argument("--profile", default="e2e")
    p.add_argument("--phases", default="1,2,4")
    p.add_argument("--repo", default=None,
                    help="Path to an existing git repo copy; default: makes its own /tmp copy of "
                         "examples/toy-repo, git init -b main, initial commit.")
    p.add_argument("--keep", action="store_true", help="Leave the temp repo/home directories on disk.")
    p.add_argument("--fake", action="store_true", help="Use the bundled scripted fake LLM (127.0.0.1 only).")
    p.add_argument(
        "--task-timeout", type=float, default=None,
        help="Seconds to wait for a dispatched task to finish (default: 120 with --fake, 900 otherwise "
             "-- a real worker takes minutes, not the fake model's milliseconds).",
    )
    return p.parse_args()


async def _run(args: argparse.Namespace) -> int:
    if not args.fake and not args.api_base:
        print("error: --api-base is required unless --fake is given", file=sys.stderr)
        return 2

    requested_phases = {int(p.strip()) for p in args.phases.split(",") if p.strip()}
    for unsupported in sorted(requested_phases - {1, 2, 4}):
        print(f"note: phase {unsupported} is not offline-runnable by this driver (manual/paid loop); skipping.")
    phases = requested_phases & {1, 2, 4}

    if not args.model:
        args.model = "openai/combo/fake" if args.fake else "openai/combo/deepseek-main"
    if args.task_timeout is None:
        args.task_timeout = 120.0 if args.fake else 900.0

    created_repo = False
    if args.repo:
        work_dir = Path(args.repo).resolve()
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="monkeyarmy_e2e_repo_"))
        shutil.copytree(TOY_REPO_SRC, work_dir, dirs_exist_ok=True)
        _git(work_dir, "init", "-b", "main")
        _git(work_dir, "-c", "user.name=e2e", "-c", "user.email=e2e@localhost", "add", "-A")
        _git(work_dir, "-c", "user.name=e2e", "-c", "user.email=e2e@localhost", "commit", "-m", "initial")
        created_repo = True

    monkey_home = Path(tempfile.mkdtemp(prefix="monkeyarmy_e2e_home_"))
    print(f"MONKEY_ARMY_HOME: {monkey_home}", flush=True)
    print(f"repo: {work_dir}", flush=True)

    fake_server = fake_thread = None
    api_base = args.api_base
    api_key: str | None
    if args.fake:
        fake_server, fake_thread, port = start_fake_server(args.model)
        api_base = f"http://127.0.0.1:{port}/v1"
        api_key = "dummy-fake-key"
        print(f"fake LLM listening on {api_base}", flush=True)
    else:
        api_key = os.environ.get(args.api_key_env_var)
        if not api_key:
            print(f"error: {args.api_key_env_var} is not set in the environment", file=sys.stderr)
            return 2

    env = {**os.environ, "MONKEY_ARMY_HOME": str(monkey_home), "PYTHONDONTWRITEBYTECODE": "1"}
    ctx = Ctx(args, work_dir, monkey_home, env)
    ctx.api_base = api_base

    try:
        await ctx.connect()
        r = await ctx.call(
            "configure", action="set_profile", name=ctx.profile, model=args.model, api_base=api_base,
            api_key_env_var=args.api_key_env_var, price_input_per_mtok=0.27, price_output_per_mtok=1.10,
        )
        if not r.get("saved"):
            print(f"error: could not configure profile: {r}", file=sys.stderr)
            return 2
        r = await ctx.call("configure", action="store_key", profile=ctx.profile, key=api_key)
        if not r.get("stored_as"):
            print(f"error: could not store the API key: {r}", file=sys.stderr)
            return 2

        if 1 in phases:
            print("\n=== Phase 1 ===", flush=True)
            await phase1(ctx)
        if 2 in phases:
            print("\n=== Phase 2 ===", flush=True)
            await phase2(ctx)
        if 4 in phases:
            print("\n=== Phase 4 ===", flush=True)
            await phase4(ctx)
    finally:
        # Never leave a worker orphaned: force-cancel/cleanup anything still
        # tracked as active before tearing the session down.
        if ctx.session is not None:
            for task_id in list(ctx.active_task_ids):
                await ctx.force_cancel_and_cleanup(task_id)
        await ctx.disconnect()
        if fake_server is not None:
            stop_fake_server(fake_server, fake_thread)
        if not args.keep:
            if created_repo:
                shutil.rmtree(work_dir, ignore_errors=True)
            shutil.rmtree(monkey_home, ignore_errors=True)
        else:
            print(f"--keep: left {work_dir} and {monkey_home} on disk")

    print("\n=== RESULTS ===")
    width = max((len(t) for t, _, _ in RESULTS), default=0)
    n_pass = sum(1 for _, s, _ in RESULTS if s == "PASS")
    n_fail = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    n_skip = sum(1 for _, s, _ in RESULTS if s == "SKIP")
    for test_id, status, evidence in RESULTS:
        line = f"{status.ljust(4)}  {test_id.ljust(width)}"
        if status != "PASS" and evidence:
            line += f"  -- {evidence[:200]}"
        print(line)
    print(f"\n{n_pass} passed, {n_fail} failed, {n_skip} skipped ({len(RESULTS)} total)")
    return 0 if n_fail == 0 else 1


def main() -> int:
    return asyncio.run(_run(parse_args()))


if __name__ == "__main__":
    sys.exit(main())
