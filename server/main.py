# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp<2"]
# ///
"""monkey-army MCP server (§6): thin registration of 13 tools (§6),
delegating everything to the stdlib-only modules beside it. Runs over stdio
via `uv run server/main.py`.

Only this module imports `mcp`; config/store/jobs/persistence/verify/
backend/worker_launcher are stdlib-only so unit tests run without any
dependency install.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

import backend
import batches
import events
import git_ops
import specs
import store
import statusline_render
import verify as verify_mod
import worker_launcher
from config import Defaults, load_defaults
from jobs import (
    changed_files,
    cleanup_job,
    create_worktree,
    get_job_with_fallback,
    persist_job,
    put_job,
    read_patch,
    repo_worktree_error,
    runtime,
    wait_for_tasks as jobs_wait_for_tasks,
)
from persistence import ACTIVE as ACTIVE_STATES, REVIEWABLE, TERMINAL as TERMINAL_STATES
from proc_utils import kill_tree
from worker_launcher import comm_dir_for, run_worker

def _cfg() -> Defaults:
    """Reload config.json's `defaults` section on every call, so
    configure(action='set_defaults') takes effect on the next tool call —
    no restart needed. A module-level `cfg = load_defaults()` here would
    freeze these values at import time (§ review-fix B)."""
    return load_defaults()


mcp = FastMCP("monkey-army")


async def _offload(fn, *args, **kwargs):
    """Run a blocking call (subprocess, HTTP, git) off the event loop.

    Several tools shell out or make HTTP calls that can take tens of
    seconds; running them inline stalls the loop, which starves worker
    stdout consumption (looks like a false "stall") and blocks
    wait_for_tasks/other tools from ticking.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


@mcp.tool(
    description=(
        "Starts an autonomous coding worker on an isolated git worktree and returns a task_id "
        "IMMEDIATELY — the worker runs in the background and you stay free. Give spec, or "
        "spec_file (+spec_section) so the server reads it from a file instead of you retyping "
        "it. Probes the profile first and refuses before creating anything. Supervise with "
        "wait_for_tasks(require=\'all\', include_results=True)."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False,
        idempotentHint=False, openWorldHint=True,
    ),
)
async def dispatch_task(
    title: str,
    spec: str = "",
    repo_path: str = "",
    spec_file: str | None = None,
    spec_section: str | None = None,
    test_command: str | None = None,
    definition_of_done: str | None = None,
    allowed_files: list[str] | None = None,
    context_files: list[str] | None = None,
    mode: str = "micro",
    profile: str | None = None,
    batch_id: str | None = None,
    batch_key: str | None = None,
    base_branch: str | None = None,
    verify_command: str | None = None,
    lint_command: str | None = None,
    max_budget_usd: float | None = None,
    max_tokens_total: int | None = None,
    timeout_s: int | None = None,
) -> str:
    cfg = _cfg()
    if mode not in ("micro", "task"):
        return json.dumps({"error": f"mode must be 'micro' or 'task', got {mode!r}"})
    if not repo_path:
        return json.dumps({"error": "repo_path is required"})

    # Spec by reference: let the server read the brief rather than have the
    # supervisor retype it as output tokens (docs/TOKEN-ECONOMICS.md).
    if spec_file:
        if spec:
            return json.dumps({"error": "pass spec or spec_file, not both"})
        try:
            spec = specs.read_spec(spec_file, spec_section)
        except ValueError as e:
            return json.dumps({"error": str(e)})
    elif not spec:
        return json.dumps({"error": "spec is required (or pass spec_file)"})

    repo_err = await _offload(repo_worktree_error, repo_path)
    if repo_err:
        return json.dumps({"error": repo_err})

    # Resolve model/key per task from the config store (facade profiles).
    # Read fresh each call so facade changes apply without a server restart.
    try:
        resolved = store.resolve_profile(profile)
    except KeyError as e:
        return json.dumps({"error": str(e)})

    # Probe BEFORE creating anything (§6.1 step 2): a dead endpoint or wrong
    # model name must never leave a worktree/branch behind for the supervisor
    # to clean up.
    probe_result = await _offload(backend.probe, resolved, ttl_s=cfg.probe_ttl_s)
    if not probe_result.get("ok"):
        return json.dumps({
            "error": f"profile {resolved['name']!r} failed its health probe: "
                     f"{probe_result.get('error', 'no choices in response')}",
            "probe": probe_result,
        })

    warnings: list[str] = []
    if not allowed_files:
        warnings.append("allowed_files is empty/unrestricted — scope enforcement will not apply to this task")
    if probe_result.get("tool_calling") == "not_observed":
        warnings.append(
            "probe did not observe a tool call from this model — deepagents relies on tool "
            "calling; verify the combo before trusting results"
        )
    if not test_command:
        warnings.append(
            "no test_command: server-side verification will pass trivially; your review is the "
            "only gate"
        )

    wt = await _offload(create_worktree, repo_path, base_branch)
    job: dict[str, Any] = {
        **wt,
        "title": title, "spec": spec,
        "specFile": spec_file, "specSection": spec_section,
        "testCommand": test_command, "definitionOfDone": definition_of_done,
        "verifyCommand": verify_command, "lintCommand": lint_command,
        "allowedFiles": allowed_files or [], "contextFiles": context_files or [],
        "mode": mode, "profile": resolved["name"],
        "status": "running", "attempt": 1, "turns": 0,
        "costUsd": None, "totalTokens": None, "priced": False, "modelsSeen": [],
        "model": resolved["model"],  # for the status line / watch stream
        # Persisted (not just passed to this run) so review_task's retry —
        # possibly after a server restart — reuses the same caps rather than
        # silently reverting to the profile's defaults.
        "maxBudgetUsd": max_budget_usd, "maxTokensTotal": max_tokens_total, "timeoutS": timeout_s,
    }
    if batch_id:
        job["batchId"] = batch_id
    if batch_key:
        job["batchKey"] = batch_key

    # From here on, any failure must not leave a worktree/branch behind for
    # the supervisor to discover and clean up by hand.
    try:
        put_job(job)
        persist_job(job)
        statusline_render.write_statusline(job)
        if batch_id and batch_key:
            try:
                batches.link(repo_path, batch_id, batch_key, wt["taskId"])
            except KeyError as e:
                warnings.append(f"batch link failed: {e}")

        # Preflight the acceptance gate BEFORE spending worker tokens: a broken
        # test RUNNER (vs merely failing assertions) makes the rubric unpassable
        # and sends the worker chasing phantom failures.
        preflight_report: dict[str, Any] | None = None
        if test_command:
            # Capped well below the MCP client's own tool timeout: a slow-but-legit
            # test command shows up as advisory timed_out, never a failed dispatch.
            preflight_report = await _offload(
                verify_mod.run_command, test_command, wt["worktree"], cfg.preflight_timeout_s
            )
            events.publish(
                wt["repo"], wt["taskId"],
                {"kind": "preflight", "note": f"test_command exit={preflight_report.get('exit_code')}"},
            )

        limits = resolved["limits"]
        args = worker_launcher.build_worker_args(job, resolved)
        # The worker runs as a background asyncio task; job state is mutated live
        # by worker_launcher (same event loop) and mirrored to disk on every change.
        run_timeout_ms = int((timeout_s or limits["timeout_s"]) * 1000)
        task = asyncio.create_task(run_worker(cfg, job, args, run_timeout_ms))
        runtime.setdefault(job["taskId"], {})["task"] = task
    except Exception as e:  # noqa: BLE001 - never leave a worktree/branch orphaned
        await _offload(cleanup_job, job)
        return json.dumps({"error": f"dispatch failed: {type(e).__name__}: {e}"})

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
            "model": resolved["model"], "profile": resolved["name"], "mode": mode,
            "warnings": warnings,
            **preflight_extra,
        }
    )


@mcp.tool(
    description=(
        "CHEAP liveness check — call often while supervising. Tiny payload: status (running, "
        "needs_input, verifying, succeeded, failed, failed_verification, failed_scope, "
        "failed_oversized, timeout, cancelled, integrated), a `done` flag, and the pending question "
        "if blocked. On 'needs_input' use answer_worker; on `done` call task_result. For files "
        "written so far and recent activity, call task_progress instead."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=False,
    ),
)
async def task_status(task_id: str) -> str:
    cfg = _cfg()
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
        payload["next"] = "task_result(task_id)"
    if j.get("error"):
        payload["error"] = j["error"]
    return json.dumps(payload)


@mcp.tool(
    description=(
        "VERBOSE progress audit — heavier than task_status, so call it occasionally (on your own "
        "estimate) or when the user asks 'how's it going?'. Reports elapsed time, step count, cost "
        "so far, the files the worker has touched in its worktree, and its most recent activity "
        "(shell commands, notes). If it looks stuck, steer_task or cancel_task; otherwise keep waiting."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=False,
    ),
)
async def task_progress(task_id: str, activity_limit: int = 8) -> str:
    cfg = _cfg()
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
        "Returns the full result of a finished task: summary, verification, scope/diff checks, "
        "patch (inlined when small enough), cost, and what to do next (review_task or "
        "cleanup_task). Works for non-succeeded tasks too — 'salvaged'/patch still show whatever "
        "work existed at failure/cancel time."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=False,
    ),
)
async def task_result(task_id: str, include_patch: bool = True) -> str:
    cfg = _cfg()
    j = get_job_with_fallback(task_id)
    if not j:
        return json.dumps({"error": "unknown task_id"})
    return json.dumps(_result_payload(j, cfg, include_patch))


def _result_payload(j: dict[str, Any], cfg: Defaults, include_patch: bool = True) -> dict[str, Any]:
    """Everything the supervisor needs to review a finished task — shared by
    task_result and wait_for_tasks(include_results=True)."""
    patch = read_patch(j["patchPath"], cfg.max_diff_lines) if include_patch and j.get("patchPath") else None
    status = j.get("status")
    return {
        "task_id": j.get("taskId"),
        "title": j.get("title"),
        "status": status,
        "attempt": j.get("attempt", 1),
        "review": j.get("review"),
        "verification": j.get("verification"),
        "scope": j.get("scope"),
        "diffstat": j.get("diffstat"),
        "patch": patch,
        "patch_path": j.get("patchPath"),
        "files_changed": j.get("filesChanged", []),
        "cost_usd": j.get("costUsd"),
        "total_tokens": j.get("totalTokens"),
        "priced": j.get("priced", False),
        "models_seen": j.get("modelsSeen", []),
        "summary": j.get("summary"),
        "error": j.get("error"),
        "salvaged": j.get("salvaged", False),
        "branch": j.get("branch"),
        "worktree": j.get("worktree"),
        "next": ("review_task(task_id, 'approve', integrate=True) | review_task(task_id, 'reject', feedback)"
                 if status == "succeeded" else "cleanup_task(task_id)"),
    }


@mcp.tool(
    description=(
        "Cancels a running task: kills the worker's whole process tree, salvages any uncommitted "
        "work onto the monkey branch (task_result then returns the patch), and marks the "
        "task 'cancelled' so cleanup_task can proceed. Use for stalled or runaway workers."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True,
        idempotentHint=True, openWorldHint=False,
    ),
)
async def cancel_task(task_id: str) -> str:
    cfg = _cfg()
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
        j["finishedAt"] = time.time()
        if await _offload(salvage_worktree, j):
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
            "note": "worktree and branch still exist; task_result for salvaged work, cleanup_task to discard",
        }
    )


@mcp.tool(
    description=(
        "Redirects a RUNNING worker at any moment, not just in reply to a question. Use to "
        "course-correct: 'do Y instead', 'skip the tests for now'. Delivered at the worker's next "
        "tool call (seconds, not instant) — it keeps working meanwhile. Overwrites any "
        "not-yet-delivered steer message; only the latest guidance is kept, so batch redirections."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False,
        idempotentHint=False, openWorldHint=False,
    ),
)
async def steer_task(task_id: str, message: str) -> str:
    cfg = _cfg()
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
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False,
        idempotentHint=False, openWorldHint=False,
    ),
)
async def answer_worker(task_id: str, answer: str) -> str:
    cfg = _cfg()
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


@mcp.tool(
    description=(
        "Removes the worktree, branch, and persisted file for a finished (non-active) task. Call "
        "once you're done with task_result's patch/summary and (if integrated) after integrate_task "
        "— nothing more can be done with the task afterward. Refuses while running/needs_input/verifying."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True,
        idempotentHint=True, openWorldHint=False,
    ),
)
async def cleanup_task(task_id: str, delete_branch: bool | None = None) -> str:
    cfg = _cfg()
    j = get_job_with_fallback(task_id)
    if not j:
        return json.dumps({"error": "unknown task_id"})
    if j.get("status") in ACTIVE_STATES:
        return json.dumps(
            {"task_id": task_id, "error": f"task is active (status: {j.get('status')}); "
                                           "abort or wait before calling cleanup_task"}
        )
    result = cleanup_job(j, delete_branch if delete_branch is not None else True)
    return json.dumps({"task_id": task_id, "cleaned": True, **result})


@mcp.tool(
    description=(
        "Records your verdict on a 'succeeded' task. 'approve' unlocks integration; with "
        "integrate=True it also merges in the same call. 'reject' (feedback >=10 chars) re-runs the "
        "worker in the SAME worktree, incrementing attempt (max 3). reviews_json — a JSON list of "
        "{task_id, verdict, feedback?, integrate?} — reviews a whole wave in one round trip."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False,
        idempotentHint=False, openWorldHint=True,
    ),
)
async def review_task(
    task_id: str = "", verdict: str = "", feedback: str | None = None,
    integrate: bool = False, integrate_mode: str | None = None,
    reviews_json: str = "",
) -> str:
    cfg = _cfg()
    if reviews_json:
        # One round trip for a whole wave of verdicts. Each separate review call
        # re-sends the supervisor's entire context, and the A/B runs measured
        # that tax as most of the orchestration cost (docs/TOKEN-ECONOMICS.md).
        try:
            rows = json.loads(reviews_json)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"reviews_json is not valid JSON: {e}"})
        if not isinstance(rows, list) or not rows:
            return json.dumps({"error": "reviews_json must be a non-empty JSON list"})
        out = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("task_id"):
                out.append({"error": f"each review needs a task_id: {row!r}"})
                continue
            out.append(json.loads(await _review_one(
                cfg, row["task_id"], row.get("verdict", ""), row.get("feedback"),
                row.get("integrate", integrate), row.get("integrate_mode", integrate_mode),
            )))
        return json.dumps({"reviews": out})
    if not task_id:
        return json.dumps({"error": "task_id is required (or pass reviews_json)"})
    return await _review_one(cfg, task_id, verdict, feedback, integrate, integrate_mode)


async def _review_one(
    cfg: Defaults, task_id: str, verdict: str, feedback: str | None,
    integrate: bool, integrate_mode: str | None,
) -> str:
    j = get_job_with_fallback(task_id)
    if not j:
        return json.dumps({"error": "unknown task_id"})
    if j.get("status") not in REVIEWABLE:
        return json.dumps({"task_id": task_id, "error": f"task not reviewable (status: {j.get('status')})"})
    if verdict not in ("approve", "reject"):
        return json.dumps({"error": f"verdict must be 'approve' or 'reject', got {verdict!r}"})

    if verdict == "approve":
        if j.get("status") != "succeeded":
            return json.dumps(
                {"task_id": task_id, "error": f"approve requires status 'succeeded' (status: {j.get('status')})"}
            )
        j["review"] = {"verdict": "approve", "feedback": feedback, "at": time.time()}
        persist_job(j)
        if integrate:
            # Approve and merge in one round trip; the review is still
            # recorded first, so I5 (no integration without approval) holds.
            merged = await _integrate(j, cfg, None, integrate_mode)
            return json.dumps({"task_id": task_id, "review": j["review"], "integrate": merged})
        return json.dumps({"task_id": task_id, "review": j["review"], "next": "integrate_task(task_id)"})

    # reject
    if not feedback or len(feedback) < 10:
        return json.dumps({"error": "reject requires feedback of at least 10 characters"})
    if not j.get("worktree") or not Path(j["worktree"]).is_dir():
        return json.dumps({"task_id": task_id, "error": "worktree no longer exists; re-dispatch instead of retrying"})
    attempt = j.get("attempt", 1)
    if attempt >= 3:
        return json.dumps({
            "task_id": task_id,
            "error": "max retry attempts (3) reached — do it yourself, or re-decompose into a fresh task",
        })

    j["review"] = {"verdict": "reject", "feedback": feedback, "at": time.time()}
    j.setdefault("feedbackHistory", []).append({"attempt": attempt, "feedback": feedback})
    j["attempt"] = attempt + 1
    j["status"] = "running"
    persist_job(j)
    statusline_render.write_statusline(j)
    events.publish(j["repo"], task_id, {"kind": "retry", "attempt": j["attempt"], "feedback": feedback[:300]})

    try:
        resolved = store.resolve_profile(j.get("profile"))
    except KeyError as e:
        return json.dumps({"error": str(e)})
    args = worker_launcher.build_worker_args(j, resolved)
    run_timeout_ms = int((j.get("timeoutS") or resolved["limits"]["timeout_s"]) * 1000)
    # A stale runtime entry (cancelled=True, an old proc handle) from the
    # attempt just rejected would otherwise survive into the retry and make
    # it finalize as cancelled the moment it produces output.
    runtime[j["taskId"]] = {}
    task = asyncio.create_task(
        worker_launcher.retry(cfg, j, args, j["feedbackHistory"], run_timeout_ms)
    )
    runtime[j["taskId"]]["task"] = task

    return json.dumps({"task_id": task_id, "attempt": j["attempt"], "status": "running"})


@mcp.tool(
    description=(
        "Waits server-side until a listed task changes status or needs input; returns at once if "
        "one already does. require=\'all\' waits for every task instead of waking on the first — "
        "prefer it, each wake re-sends your whole context. include_results=True attaches each "
        "finished task\'s result, saving a task_result call. Hard cap 170s."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=False,
    ),
)
async def wait_for_tasks(
    task_ids: list[str], timeout_s: int | None = None, include_results: bool = False,
    require: str = "any",
) -> str:
    cfg = _cfg()
    if require not in ("any", "all"):
        return json.dumps({"error": f"require must be 'any' or 'all', got {require!r}"})
    result = await jobs_wait_for_tasks(
        task_ids, timeout_s, cfg.wait_timeout_s, cfg.wait_hard_cap_s, require
    )
    if include_results:
        # Every supervisor round trip re-sends its whole context. Folding the
        # finished tasks' results into the wait saves one task_result call
        # per task (measured live: 28 supervisor requests for 3 small tasks).
        for row in result.get("tasks", []):
            if row.get("done"):
                j = get_job_with_fallback(row["task_id"])
                if j:
                    row["result"] = _result_payload(j, cfg)
    return json.dumps(result)


@mcp.tool(
    description=(
        "Merges an approved task's patch into repo_path's CURRENT branch: dry-run checked first, "
        "then applied atomically (never half-merged) and, in 'commit' mode, committed under the "
        "user's own git identity. Requires review_task(verdict='approve') first. On conflict, "
        "nothing changes — re-dispatch against the current branch and integrate again."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False,
        idempotentHint=False, openWorldHint=False,
    ),
)
async def integrate_task(
    task_id: str, message: str | None = None, mode: str | None = None,
    allow_branch_mismatch: bool = False,
) -> str:
    cfg = _cfg()
    j = get_job_with_fallback(task_id)
    if not j:
        return json.dumps({"error": "unknown task_id"})
    return json.dumps(await _integrate(j, cfg, message, mode, allow_branch_mismatch))


async def _integrate(
    j: dict[str, Any], cfg: Defaults, message: str | None, mode: str | None,
    allow_branch_mismatch: bool = False,
) -> dict[str, Any]:
    """Shared by integrate_task and review_task(approve, integrate=True)."""
    try:
        return await _offload(
            git_ops.integrate, j, j["repo"], message, mode or cfg.integrate_mode, allow_branch_mismatch
        )
    except Exception as e:  # noqa: BLE001 - never leak a raw traceback to the supervisor
        return {
            "task_id": j.get("taskId"), "integrated": False, "reason": "error",
            "detail": f"{type(e).__name__}: {e}",
        }


@mcp.tool(
    description=(
        "Manages a multi-task batch. create(repo_path, goal, tasks_json) validates dependencies/"
        "scope overlap and returns parallel waves; status(batch_id) shows progress; finish(batch_id, "
        "verify_command?) integrates every approved task in dependency order and reports totals — "
        "repo_path is optional for status/finish (found from batch_id). tasks_json: JSON list of "
        "{key, title, dependsOn?, allowedFiles?}."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False,
        idempotentHint=False, openWorldHint=False,
    ),
)
async def batch(
    # *_json params are plain `str`, not `str | None`: mcp<2 only skips its
    # JSON pre-parse for fields annotated exactly `str`, so an Optional
    # turned the caller's JSON string into a list and failed validation.
    action: str, repo_path: str | None = None, batch_id: str | None = None,
    goal: str | None = None, tasks_json: str = "",
    verify_command: str | None = None, mode: str | None = None,
) -> str:
    cfg = _cfg()
    if action == "create":
        if not repo_path or not goal or not tasks_json:
            return json.dumps({"error": "create requires repo_path, goal, tasks_json"})
        try:
            tasks = json.loads(tasks_json)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"invalid tasks_json: {e}"})
        if not isinstance(tasks, list):
            return json.dumps({"error": "tasks_json must be a JSON list"})
        try:
            return json.dumps(batches.create(repo_path, goal, tasks, verify_command, mode))
        except ValueError as e:
            return json.dumps({"error": str(e)})

    if action == "status":
        if not batch_id:
            return json.dumps({"error": "status requires batch_id"})
        if not repo_path:
            found = await _offload(batches.find_manifest, batch_id)
            if found is None:
                return json.dumps({"error": f"unknown batch_id {batch_id!r}"})
            repo_path = found[0]
        try:
            return json.dumps(batches.status(repo_path, batch_id))
        except KeyError as e:
            return json.dumps({"error": str(e)})

    if action == "finish":
        if not batch_id:
            return json.dumps({"error": "finish requires batch_id"})
        if not repo_path:
            found = await _offload(batches.find_manifest, batch_id)
            if found is None:
                return json.dumps({"error": f"unknown batch_id {batch_id!r}"})
            repo_path = found[0]
        try:
            return json.dumps(await _offload(batches.finish, repo_path, batch_id, verify_command, mode, cfg))
        except KeyError as e:
            return json.dumps({"error": str(e)})

    return json.dumps({"error": f"unknown action {action!r}"})


# ── Configuration facade (§6.13) ─────────────────────────────────────────
# Sovereignty rule (I9): the supervisor must only call configure's mutating
# actions when the user explicitly asked for a configuration change —
# enforced by the packaged skill and restated in the tool description below,
# since a skill rule alone is easy for a model to drift past under pressure.

from pydantic import BaseModel  # noqa: E402 - transitive dependency of mcp


class _ApiKeyInput(BaseModel):
    api_key: str


def _parse_json_arg(raw: str | None, label: str) -> tuple[dict[str, Any] | None, str | None]:
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"invalid {label}: {e}"
    if not isinstance(parsed, dict):
        return None, f"{label} must be a JSON object"
    return parsed, None


async def _configure_store_key(
    profile: str | None, key: str | None, api_key_env_var: str | None = None
) -> str:
    # Credentials are keyed by env var name, not by profile, so a key can be
    # stored before any profile exists — which lets the setup wizard collect
    # the key and list the combos BEFORE writing a profile, instead of
    # creating a placeholder one it has to repair later.
    if profile:
        cfg_store = store.load_store()
        prof = cfg_store["profiles"].get(profile)
        if not prof:
            return json.dumps({"error": f"unknown profile {profile!r}"})
        env_var = prof.get("api_key_env_var")
        if not env_var:
            return json.dumps({"error": f"profile {profile!r} has no api_key_env_var"})
    else:
        env_var = api_key_env_var
        if not env_var:
            return json.dumps({"error": "store_key requires profile or api_key_env_var"})

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
    # Other profiles can share this env var, so clear every cached probe
    # rather than guessing which ones are affected.
    backend.invalidate_probe()
    note = None
    if via == "parameter":
        note = "key transited the model conversation; consider rotating it and re-entering via elicitation"
    return json.dumps({"profile": profile, "stored_as": env_var, "via": via, "note": note})


@mcp.tool(
    description=(
        "Only call mutating actions when the user explicitly asked for a configuration change. "
        "Manages worker profiles, defaults, API keys, and diagnostics: status, set_profile, "
        "remove_profile, set_default, set_defaults, store_key, discover_models, probe, doctor, "
        "add_note, prune, reset. action selects the operation; other args vary per action."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True,
        idempotentHint=False, openWorldHint=True,
    ),
)
async def configure(
    action: str,
    name: str | None = None,
    model: str | None = None,
    api_base: str | None = None,
    api_key_env_var: str | None = None,
    fallback_models: list[str] | None = None,
    price_input_per_mtok: float | None = None,
    price_output_per_mtok: float | None = None,
    model_kwargs_json: str = "",
    limits_json: str = "",
    profile: str | None = None,
    key: str | None = None,
    repo_path: str | None = None,
    text: str | None = None,
    older_than_days: int | None = None,
    defaults_json: str = "",
) -> str:
    cfg = _cfg()
    if action == "status":
        cfg_store = store.load_store()
        profiles = {
            n: {**prof, "auth": store.auth_state(prof)}
            for n, prof in cfg_store["profiles"].items()
        }
        return json.dumps({
            "config_path": str(store.config_path()),
            "default_profile": cfg_store["default_profile"],
            "profiles": profiles,
            "defaults": store.get_defaults(),
            "last_probes": backend.all_last_probes(),
            "last_doctor_at": cfg_store.get("meta", {}).get("last_doctor_at"),
        })

    if action == "set_profile":
        if not name or not model:
            return json.dumps({"error": "set_profile requires name and model"})
        model_kwargs, err = _parse_json_arg(model_kwargs_json, "model_kwargs_json")
        if err:
            return json.dumps({"error": err})
        limits, err = _parse_json_arg(limits_json, "limits_json")
        if err:
            return json.dumps({"error": err})
        prices = {}
        if price_input_per_mtok is not None:
            prices["input"] = price_input_per_mtok
        if price_output_per_mtok is not None:
            prices["output"] = price_output_per_mtok
        try:
            prof = store.set_profile(
                name, model, api_key_env_var, api_base, fallback_models,
                prices or None, model_kwargs or None, limits or None,
            )
        except ValueError as e:
            return json.dumps({"error": str(e)})
        backend.invalidate_probe(name)
        return json.dumps({"profile": name, "saved": True, **prof})

    if action == "remove_profile":
        if not name:
            return json.dumps({"error": "remove_profile requires name"})
        if not store.remove_profile(name):
            return json.dumps({"error": f"unknown profile {name!r}"})
        backend.invalidate_probe(name)
        return json.dumps({"profile": name, "removed": True})

    if action == "set_default":
        if not name:
            return json.dumps({"error": "set_default requires name"})
        try:
            store.set_default_profile(name)
        except KeyError:
            return json.dumps({"error": f"unknown profile {name!r}"})
        backend.invalidate_probe(name)
        return json.dumps({"default_profile": name})

    if action == "set_defaults":
        patch, err = _parse_json_arg(defaults_json, "defaults_json")
        if err:
            return json.dumps({"error": err})
        return json.dumps({"defaults": store.set_defaults(patch or {})})

    if action == "store_key":
        if not profile and not api_key_env_var:
            return json.dumps({"error": "store_key requires profile or api_key_env_var"})
        return await _configure_store_key(profile, key, api_key_env_var)

    if action == "discover_models":
        # Either against a saved profile, or against a URL + key env var that
        # has no profile yet (first-run wizard).
        if profile or not api_base:
            try:
                resolved = store.resolve_profile(profile)
            except KeyError as e:
                return json.dumps({"error": str(e)})
        else:
            env_var = api_key_env_var or "MONKEY_9ROUTER_KEY"
            resolved = {
                "api_base": api_base,
                "api_key": store.get_credential(env_var) or os.environ.get(env_var),
            }
        return json.dumps(await _offload(backend.discover_models, resolved))

    if action == "probe":
        try:
            resolved = store.resolve_profile(profile)
        except KeyError as e:
            return json.dumps({"error": str(e)})
        return json.dumps(await _offload(backend.probe, resolved, ttl_s=cfg.probe_ttl_s))

    if action == "doctor":
        checks = await _offload(backend.doctor, repo_path)
        last_doctor_at = await _offload(store.record_doctor_run)
        return json.dumps({"checks": checks, "last_doctor_at": last_doctor_at})

    if action == "add_note":
        if not repo_path or not text:
            return json.dumps({"error": "add_note requires repo_path and text"})
        try:
            store.append_note(repo_path, text)
        except ValueError as e:
            return json.dumps({"error": str(e)})
        return json.dumps({"repo_path": repo_path, "added": True})

    if action == "prune":
        repos = [str(Path(repo_path).resolve())] if repo_path else store.all_repos()
        results = [await _offload(store.prune_repo, r, older_than_days or 14) for r in repos]
        return json.dumps({"pruned": results})

    if action == "reset":
        # Deliberately two-step: a wrong profile is fixed by re-running
        # set_profile, so a full wipe is only ever what someone asked for
        # explicitly. `text="confirm"` is that second step.
        if text != "confirm":
            cfg_store = store.load_store()
            return json.dumps({
                "error": "reset deletes every profile and stored key; call again with text='confirm'",
                "would_delete": {
                    "profiles": sorted(cfg_store["profiles"]),
                    "config_path": str(store.config_path()),
                    "credentials_path": str(store.credentials_path()),
                },
                "note": "to change one profile, use set_profile (same name overwrites) or remove_profile",
            })
        removed = store.reset_config()
        return json.dumps({"reset": True, **removed})

    return json.dumps({"error": f"unknown action {action!r}"})


if __name__ == "__main__":
    mcp.run()
