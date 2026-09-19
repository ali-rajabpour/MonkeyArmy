# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp<2"]
# ///
"""monkey-army MCP server, Python edition (parallel implementation of
src/mcp-server.ts — same four tools, same response shapes, same persisted-job
format). Runs over stdio via `uv run server/main.py`.

Only this module imports `mcp`; config/jobs/persistence/worker_launcher are
stdlib-only so unit tests run without any dependency install.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP

import events
import store
import statusline_render
import verify as verify_mod
from config import load_defaults
from jobs import (
    changed_files,
    cleanup_job,
    create_worktree,
    get_job_with_fallback,
    persist_job,
    put_job,
    runtime,
)
from persistence import TERMINAL as TERMINAL_STATES
from proc_utils import kill_tree
from worker_launcher import comm_dir_for, run_worker

cfg = load_defaults()
mcp = FastMCP("monkey-army")


@mcp.tool(
    description=(
        "Starts an autonomous coding worker on an isolated git worktree and returns a task_id "
        "IMMEDIATELY — the worker runs in the background and you stay free. MCP cannot push into "
        "your context, so supervise by polling: end your turn and re-check on a cadence you "
        "schedule (or when the user asks). Use the cheap get_task_status for liveness "
        "(running / needs_input / done), get_task_progress for an occasional deeper audit "
        "(files written, recent activity), answer_worker to unblock a question, and "
        "fetch_task_result when done. max_budget_usd caps accumulated spend (default $5) — the "
        "worker stops itself and reports what it had once the cap is crossed, rather than "
        "running unbounded."
    )
)
async def run_dev_task(
    spec: str,
    repo_path: str,
    test_command: str | None = None,
    definition_of_done: str | None = None,
    base_branch: str | None = None,
    recursion_limit: int | None = None,
    timeout_ms: int | None = None,
    profile: str | None = None,
    max_budget_usd: float | None = None,
) -> str:
    # Resolve model/key per task from the config store (facade profiles).
    # Read fresh each call so facade changes apply without a server restart.
    try:
        resolved = store.resolve_profile(profile)
    except KeyError as e:
        return json.dumps({"error": str(e)})

    wt = create_worktree(repo_path, base_branch)
    job: dict[str, Any] = {
        **wt,
        "status": "running",
        "turns": 0,
        "costUsd": None,
        "totalTokens": None,
        "model": resolved["model"],  # for the status line / watch stream
    }
    put_job(job)
    persist_job(job)
    statusline_render.write_statusline(job)

    # Preflight the acceptance gate BEFORE spending worker tokens: a broken
    # test RUNNER (vs merely failing assertions) makes the rubric unpassable
    # and sends the worker chasing phantom failures.
    preflight_report: dict[str, Any] | None = None
    if test_command:
        # 60s cap: run_dev_task must return well within the MCP client's own
        # tool timeout; a slow-but-legit test command shows up as advisory
        # timed_out, never as a failed delegation start.
        preflight_report = await asyncio.get_event_loop().run_in_executor(
            None, verify_mod.run_test_command, test_command, wt["worktree"], 60
        )
        events.publish(
            wt["repo"], wt["taskId"],
            {"kind": "preflight", "note": f"test_command exit={preflight_report.get('exit_code')}"},
        )

    limits = resolved["limits"]
    args = {
        "spec": spec,
        "worktree": wt["worktree"],
        "test_command": test_command,
        "definition_of_done": definition_of_done,
        "recursion_limit": recursion_limit or limits["recursion_limit_task"],
        "rubric_max_iterations": limits["rubric_max_iterations_task"],
        "model": resolved["model"],
        "api_key_env_var": resolved["api_key_env_var"],
        "api_key": resolved["api_key"],
        "fallback_models": resolved["fallback_models"],
        "max_budget_usd": max_budget_usd or limits["max_budget_usd"],
    }
    # The worker runs as a background asyncio task; job state is mutated live
    # by worker_launcher (same event loop) and mirrored to disk on every change.
    run_timeout_ms = timeout_ms or int(limits["timeout_s"] * 1000)
    task = asyncio.create_task(run_worker(cfg, job, args, run_timeout_ms))
    runtime.setdefault(job["taskId"], {})["task"] = task

    # A broken test RUNNER makes the acceptance gate unpassable — surface it up
    # front so the supervisor can abort before the worker wastes tokens on it.
    preflight_extra: dict[str, Any] = {}
    if preflight_report is not None:
        preflight_extra["preflight"] = preflight_report
        note = verify_mod.preflight_note(preflight_report)
        if note:
            preflight_extra["preflight_note"] = note

    return json.dumps(
        {
            "task_id": wt["taskId"], "status": "running",
            "branch": wt["branch"], "worktree": wt["worktree"],
            "model": resolved["model"], "model_source": resolved["source"],
            "note": "worker running in the background — you are free to continue. Don't block: end "
                    "your turn and re-check on a cadence you schedule (or when the user asks). "
                    "get_task_status is cheap for liveness; get_task_progress audits deeper; "
                    "answer_worker unblocks a 'needs_input' question; fetch_task_result when done.",
            **preflight_extra,
        }
    )


@mcp.tool(
    description=(
        "CHEAP liveness check — call this often while supervising. Returns a tiny payload: the "
        "status (running / needs_input / succeeded / failed / timeout / cancelled), a `done` flag, "
        "and, if the worker is blocked, the pending question. Keep polling on your own cadence; on "
        "'needs_input' answer with answer_worker; on `done` call fetch_task_result. For a deeper "
        "look (files written so far, recent activity) call get_task_progress instead."
    )
)
async def get_task_status(task_id: str) -> str:
    j = get_job_with_fallback(task_id)
    if not j:
        return json.dumps({"error": "unknown task_id"})
    status = j.get("status")
    payload: dict[str, Any] = {
        "task_id": task_id,
        "status": status,
        "done": status in TERMINAL_STATES,
    }
    q = j.get("question")
    if status == "needs_input" and q:
        payload["question"] = {"id": q.get("id"), "message": q.get("message")}
        payload["action_required"] = (
            "answer_worker(task_id, answer) — answer from your own context, or relay to the user "
            "first if it's genuinely their decision"
        )
    if status in TERMINAL_STATES:
        payload["next"] = "fetch_task_result(task_id)"
    if j.get("error"):
        payload["error"] = j["error"]
    return json.dumps(payload)


@mcp.tool(
    description=(
        "VERBOSE progress audit — heavier than get_task_status, so call it occasionally (on your "
        "own estimate) or when the user asks 'how's it going?'. Reports elapsed time, step count, "
        "cost so far, the files the worker has touched in its worktree, and its most recent "
        "activity (shell commands, notes) so you can actually see what it's doing."
    )
)
async def get_task_progress(task_id: str, activity_limit: int = 12) -> str:
    j = get_job_with_fallback(task_id)
    if not j:
        return json.dumps({"error": "unknown task_id"})

    payload: dict[str, Any] = {
        "task_id": task_id,
        "status": j.get("status"),
        "step": j.get("lastStep") or j.get("turns", 0),
        "cost_usd": j.get("costUsd"),
        "latest_note": j.get("progress"),
    }
    if j.get("startedAt"):
        payload["elapsed_s"] = round(time.time() - j["startedAt"])
    if j.get("lastActivityTs"):
        payload["last_activity_age_s"] = round(time.time() - j["lastActivityTs"])

    # Live audit of what the worker has actually written in its worktree.
    if j.get("worktree"):
        payload["files_touched"] = changed_files(j["worktree"])

    # Recent activity, newest last, rendered the same way the events feed does.
    recent = events.read_log(j.get("repo", ""), task_id, limit=activity_limit)
    payload["recent_activity"] = [events.event_message(e) for e in recent]

    if j.get("question"):
        payload["question"] = j["question"]
    if j.get("salvaged"):
        payload["salvaged"] = True
    if j.get("error"):
        payload["error"] = j["error"]
    return json.dumps(payload)


@mcp.tool(
    description=(
        "Returns summary, patch, files changed, tests and cost of a finished task. Works for "
        "non-succeeded tasks too: when 'salvaged' is true, the patch contains the worker's "
        "uncommitted work preserved at failure/cancel time — review it before re-delegating."
    )
)
async def fetch_task_result(task_id: str) -> str:
    j = get_job_with_fallback(task_id)
    if not j:
        return json.dumps({"error": "unknown task_id"})
    return json.dumps(
        {
            "task_id": task_id,
            "status": j.get("status"),
            "summary": j.get("summary"),
            "patch_path": j.get("patchPath"),
            "files_changed": j.get("filesChanged", []),
            "tests": j.get("tests", {}),
            "cost_usd": j.get("costUsd"),
            "total_tokens": j.get("totalTokens"),
            "num_turns": j.get("turns", 0),
            "branch": j.get("branch"),
            "worktree": j.get("worktree"),
            "salvaged": j.get("salvaged", False),
            "error": j.get("error"),
        }
    )


@mcp.tool(
    description=(
        "Cancels a running task: kills the worker's whole process tree, salvages any uncommitted "
        "work onto the monkey branch (fetch_task_result then returns the patch), and marks the "
        "task 'cancelled' so cleanup_task can proceed. Use for stalled or runaway workers."
    )
)
async def cancel_task(task_id: str) -> str:
    j = get_job_with_fallback(task_id)
    if not j:
        return json.dumps({"error": "unknown task_id"})
    if j.get("status") != "running" and j.get("status") != "needs_input":
        return json.dumps(
            {"task_id": task_id, "error": f"task is not running (status: {j.get('status')})"}
        )

    rt = runtime.get(task_id) or {}
    rt["cancelled"] = True
    runtime[task_id] = rt

    proc = rt.get("proc")
    pid = getattr(proc, "pid", None) or j.get("workerPid")
    if pid:
        kill_tree(pid)

    task = rt.get("task")
    if task is not None:
        # run_worker sees EOF, notices rt["cancelled"], finalizes as
        # 'cancelled' and salvages — wait for that instead of racing it.
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=30)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        j = get_job_with_fallback(task_id) or j
    else:
        # Job from a previous server process: no runtime handle. The tree
        # kill above (via persisted workerPid) is all we can do; finalize
        # the record directly.
        from jobs import salvage_worktree

        j["status"] = "cancelled"
        j["error"] = "cancelled by supervisor (stale job from a previous server session)"
        if salvage_worktree(j):
            j["salvaged"] = True
        persist_job(j)
        events.publish(
            j["repo"], task_id,
            {"kind": "cancelled", "error": j["error"], "salvaged": j.get("salvaged", False)},
        )

    return json.dumps(
        {
            "task_id": task_id,
            "status": j.get("status"),
            "salvaged": j.get("salvaged", False),
            "patch_path": j.get("patchPath"),
            "note": "worktree and branch still exist; fetch_task_result for salvaged work, cleanup_task to discard",
        }
    )


@mcp.tool(
    description=(
        "Proactively redirects a RUNNING worker at any moment — not just in reply to a question "
        "it asked. Use to course-correct: 'stop implementing X, do Y instead', 'skip the tests for "
        "now', 'the file should be named differently'. Delivered opportunistically at the worker's "
        "next tool call (typically within seconds, not instantaneous) — it keeps working in the "
        "meantime. Overwrites any not-yet-delivered steer message for this task; only the latest "
        "guidance is kept, so batch related redirections into one call."
    )
)
async def steer_task(task_id: str, message: str) -> str:
    j = get_job_with_fallback(task_id)
    if not j:
        return json.dumps({"error": "unknown task_id"})
    if j.get("status") not in ("running", "needs_input"):
        return json.dumps(
            {"task_id": task_id, "error": f"task is not active (status: {j.get('status')})"}
        )

    comm_dir = comm_dir_for(j)
    try:
        comm_dir.mkdir(parents=True, exist_ok=True)
        steer_path = comm_dir / "steer.json"
        tmp_path = comm_dir / "steer.json.tmp"
        tmp_path.write_text(json.dumps({"message": message}), encoding="utf-8")
        tmp_path.replace(steer_path)  # atomic: the worker never reads a half-written file
    except OSError as e:
        return json.dumps({"task_id": task_id, "error": f"could not write steer message: {e}"})

    events.publish(j["repo"], task_id, {"kind": "steer", "message": message[:300]})
    return json.dumps(
        {
            "task_id": task_id, "delivered": "pending",
            "note": "delivered to the worker at its next tool call, not instantaneous",
        }
    )


@mcp.tool(
    description=(
        "Answers a worker that is blocked in status 'needs_input' (it called ask_supervisor or "
        "report_blocker). The answer is delivered out-of-band and the worker resumes immediately. "
        "If the question is a product/user decision, relay it to the user before answering."
    )
)
async def answer_worker(task_id: str, answer: str) -> str:
    j = get_job_with_fallback(task_id)
    if not j:
        return json.dumps({"error": "unknown task_id"})
    question = j.get("question")
    if j.get("status") != "needs_input" or not question:
        return json.dumps(
            {
                "task_id": task_id,
                "error": f"no pending question (status: {j.get('status')})",
            }
        )

    comm_dir = comm_dir_for(j)
    try:
        comm_dir.mkdir(parents=True, exist_ok=True)
        answer_path = comm_dir / f"{question['id']}.json"
        tmp_path = comm_dir / f"{question['id']}.json.tmp"
        tmp_path.write_text(json.dumps({"answer": answer}), encoding="utf-8")
        tmp_path.replace(answer_path)  # atomic: worker never reads a half-written file
    except OSError as e:
        return json.dumps({"task_id": task_id, "error": f"could not write answer: {e}"})

    j["status"] = "running"
    j["lastQuestion"] = j.pop("question")
    # Reset the stall clock: without this, lastActivityTs is still the moment
    # the question was ASKED (which may have been many minutes ago), and the
    # worker_launcher stall watchdog would treat the resumed run as already
    # expired the instant it stops being "needs_input".
    j["lastActivityTs"] = time.time()
    persist_job(j)
    events.publish(
        j["repo"], task_id,
        {"kind": "answer", "question_id": question["id"], "answer": answer[:300]},
    )
    return json.dumps({"task_id": task_id, "delivered": True, "question_id": question["id"]})


@mcp.tool(description="Removes the worktree, branch, and persisted file for a finished task.")
async def cleanup_task(task_id: str, delete_branch: bool | None = None) -> str:
    j = get_job_with_fallback(task_id)
    if not j:
        return json.dumps({"error": "unknown task_id"})
    if j.get("status") == "running":
        return json.dumps(
            {"task_id": task_id, "error": "task is still running; abort or wait before calling cleanup_task"}
        )
    result = cleanup_job(j, delete_branch if delete_branch is not None else True)
    return json.dumps({"task_id": task_id, "cleaned": True, **result})


# ── Configuration facade ─────────────────────────────────────────────────
# Sovereignty rule: the supervisor must only call these tools when the user
# explicitly asked for a configuration change (enforced by the packaged skill).

from pydantic import BaseModel  # noqa: E402 - transitive dependency of mcp


class _ApiKeyInput(BaseModel):
    api_key: str


@mcp.tool(
    description=(
        "Shows the worker configuration state: model profiles, the default profile, "
        "per-profile auth availability (API key reachable), and the config file location. "
        "Read-only."
    )
)
async def provider_status() -> str:
    cfg_store = store.load_store()
    profiles = {
        name: {**prof, "auth": store.auth_state(prof)}
        for name, prof in cfg_store["profiles"].items()
    }
    return json.dumps(
        {
            "config_path": str(store.config_path()),
            "default_profile": cfg_store["default_profile"],
            "profiles": profiles,
            "defaults": store.get_defaults(),
        }
    )


@mcp.tool(
    description=(
        "Creates or updates a named model profile in the persistent config store. Applies "
        "immediately (no Claude Code restart). Only call when the user explicitly asked to "
        "add or change a profile. Optional fallback_models (same 'provider:model' convention "
        "as model, e.g. ['litellm:openai/combo-fallback']) are tried in order via litellm's "
        "own fallback mechanism if the primary model's call fails — only set this when the "
        "user explicitly asked for fallback/backup models."
    )
)
async def set_model_profile(
    name: str,
    model: str,
    api_key_env_var: str | None = None,
    api_base: str | None = None,
    fallback_models: list[str] | None = None,
) -> str:
    try:
        prof = store.set_profile(name, model, api_key_env_var, api_base, fallback_models)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    return json.dumps({"profile": name, "saved": True, **prof})


@mcp.tool(description="Removes a named model profile. Only on explicit user request.")
async def remove_model_profile(name: str) -> str:
    if not store.remove_profile(name):
        return json.dumps({"error": f"unknown profile {name!r}"})
    return json.dumps({"profile": name, "removed": True})


@mcp.tool(description="Sets the default model profile. Only on explicit user request.")
async def set_default_profile(name: str) -> str:
    try:
        store.set_default_profile(name)
    except KeyError:
        return json.dumps({"error": f"unknown profile {name!r}"})
    return json.dumps({"default_profile": name})


@mcp.tool(
    description=(
        "Stores an API key for a profile's api_key_env_var. Preferred path: call WITHOUT the "
        "'key' argument — the server then asks the user directly through an MCP elicitation "
        "dialog, so the secret never enters the model's conversation context. Passing 'key' "
        "as an argument works but the value transits the conversation; warn the user."
    )
)
async def store_api_key(profile: str, key: str | None = None) -> str:
    cfg_store = store.load_store()
    prof = cfg_store["profiles"].get(profile)
    if not prof:
        return json.dumps({"error": f"unknown profile {profile!r}"})
    env_var = prof.get("api_key_env_var")
    if not env_var:
        return json.dumps({"error": f"profile {profile!r} has no api_key_env_var"})

    via = "parameter"
    if key is None:
        # Elicitation: the response returns straight to this server via the
        # client UI — the model never sees the secret.
        try:
            ctx = mcp.get_context()
            result = await ctx.elicit(
                message=(
                    f"Enter the API key to store for profile '{profile}' "
                    f"(saved to {store.credentials_path()} as {env_var})."
                ),
                schema=_ApiKeyInput,
            )
            if getattr(result, "action", None) != "accept":
                return json.dumps({"cancelled": True, "profile": profile})
            key = result.data.api_key
            via = "elicitation"
        except Exception as e:  # noqa: BLE001 - client may not support elicitation
            return json.dumps(
                {
                    "error": "elicitation unavailable on this client",
                    "detail": str(e)[:200],
                    "fallback": (
                        f"Set the {env_var} environment variable before launching Claude Code, "
                        f"or add {{\"{env_var}\": \"<key>\"}} to {store.credentials_path()}."
                    ),
                }
            )
    if not key or not isinstance(key, str):
        return json.dumps({"error": "no key provided"})

    store.store_credential(env_var, key)
    note = None
    if via == "parameter":
        note = "key transited the model conversation; consider rotating it and re-entering via elicitation"
    return json.dumps({"profile": profile, "stored_as": env_var, "via": via, "note": note})


@mcp.tool(
    description="Reports whether an API key is reachable on disk/env for a profile. Read-only."
)
async def auth_status(profile: str) -> str:
    cfg_store = store.load_store()
    prof = cfg_store["profiles"].get(profile)
    if not prof:
        return json.dumps({"error": f"unknown profile {profile!r}"})
    return json.dumps({"profile": profile, **store.auth_state(prof)})


if __name__ == "__main__":
    mcp.run()
