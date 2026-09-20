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
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP

import backend
import batches
import config
import events
import git_ops
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
    """Reload the env-only defaults on every call, so a MONKEY_* variable
    changed in the shell takes effect on the next tool call (after a
    restart — env vars are fixed for the life of this process, unlike the
    old config.json). A module-level `cfg = load_defaults()` here would
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
        "IMMEDIATELY — the worker runs in the background and you stay free. Checks env config and "
        "probes the endpoint first, refusing before creating anything if either fails. "
        "allowed_files unrestricted is allowed but returned as a warning. Supervise with "
        "wait_for_tasks, not polling; review with task_result then review_task/integrate_task."
    )
)
async def dispatch_task(
    title: str,
    spec: str,
    repo_path: str,
    test_command: str | None = None,
    definition_of_done: str | None = None,
    allowed_files: list[str] | None = None,
    context_files: list[str] | None = None,
    mode: str = "micro",
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

    # Env config resolved fresh on every call (config.py is the only source —
    # env-config-spec.md). Every missing/invalid required variable is
    # reported together, before anything is created, and before the repo
    # check: a machine that isn't configured at all should say so first.
    resolved = config.required_config()
    if resolved["errors"]:
        return json.dumps({"error": "; ".join(resolved["errors"])})

    repo_err = await _offload(repo_worktree_error, repo_path)
    if repo_err:
        return json.dumps({"error": repo_err})

    # Probe BEFORE creating anything (§6.1 step 2): a dead endpoint or wrong
    # model name must never leave a worktree/branch behind for the supervisor
    # to clean up.
    probe_result = await _offload(backend.probe, _probe_profile(resolved), ttl_s=cfg.probe_ttl_s)
    if not probe_result.get("ok"):
        return json.dumps({
            "error": f"health probe failed for model {resolved['model']!r}: "
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
        "testCommand": test_command, "definitionOfDone": definition_of_done,
        "verifyCommand": verify_command, "lintCommand": lint_command,
        "allowedFiles": allowed_files or [], "contextFiles": context_files or [],
        "mode": mode,
        "status": "running", "attempt": 1, "turns": 0,
        "costUsd": None, "totalTokens": None, "priced": False, "modelsSeen": [],
        "model": resolved["model"],  # for the status line / watch stream
        # Persisted (not just passed to this run) so review_task's retry —
        # possibly after a server restart — reuses the same caps rather than
        # silently reverting to the env defaults.
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

        args = worker_launcher.build_worker_args(job, cfg, resolved)
        # The worker runs as a background asyncio task; job state is mutated live
        # by worker_launcher (same event loop) and mirrored to disk on every change.
        run_timeout_ms = int((timeout_s or cfg.timeout_s) * 1000)
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
            "model": resolved["model"], "mode": mode,
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
    )
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
    )
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
    )
)
async def task_result(task_id: str, include_patch: bool = True) -> str:
    cfg = _cfg()
    j = get_job_with_fallback(task_id)
    if not j:
        return json.dumps({"error": "unknown task_id"})
    diffstat = j.get("diffstat")
    patch = read_patch(j["patchPath"], cfg.max_diff_lines) if include_patch and j.get("patchPath") else None
    status = j.get("status")
    return json.dumps(
        {
            "task_id": task_id,
            "title": j.get("title"),
            "status": status,
            "attempt": j.get("attempt", 1),
            "review": j.get("review"),
            "verification": j.get("verification"),
            "scope": j.get("scope"),
            "diffstat": diffstat,
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
            "next": ("review_task(task_id, 'approve'|'reject', feedback)"
                     if status == "succeeded" else "cleanup_task(task_id)"),
        }
    )


@mcp.tool(
    description=(
        "Cancels a running task: kills the worker's whole process tree, salvages any uncommitted "
        "work onto the monkey branch (task_result then returns the patch), and marks the "
        "task 'cancelled' so cleanup_task can proceed. Use for stalled or runaway workers."
    )
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
    )
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
    )
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
    )
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
        "Records your verdict on a finished ('succeeded') task. 'approve' unlocks integrate_task "
        "— nothing merges without this. 'reject' (needs feedback, >=10 chars) re-spawns the worker "
        "in the SAME worktree with your feedback appended to the brief, incrementing attempt "
        "(max 3 — beyond that, do it yourself or re-decompose into a fresh task)."
    )
)
async def review_task(task_id: str, verdict: str, feedback: str | None = None) -> str:
    cfg = _cfg()
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

    resolved = config.required_config()
    if resolved["errors"]:
        return json.dumps({"error": "; ".join(resolved["errors"])})
    args = worker_launcher.build_worker_args(j, cfg, resolved)
    run_timeout_ms = int((j.get("timeoutS") or cfg.timeout_s) * 1000)
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
        "Waits, sleeping in 1s ticks server-side, until any listed task changes status or needs "
        "input; returns immediately if one already does. Prefer this over polling; each poll turn "
        "re-sends your whole context. Hard cap 170s per call — call again to keep waiting on tasks "
        "still running."
    )
)
async def wait_for_tasks(task_ids: list[str], timeout_s: int | None = None) -> str:
    cfg = _cfg()
    result = await jobs_wait_for_tasks(task_ids, timeout_s, cfg.wait_timeout_s, cfg.wait_hard_cap_s)
    return json.dumps(result)


@mcp.tool(
    description=(
        "Merges an approved task's patch into repo_path's CURRENT branch: dry-run checked first, "
        "then applied atomically (never half-merged) and, in 'commit' mode, committed under the "
        "user's own git identity. Requires review_task(verdict='approve') first. On conflict, "
        "nothing changes — re-dispatch against the current branch and integrate again."
    )
)
async def integrate_task(
    task_id: str, message: str | None = None, mode: str | None = None,
    allow_branch_mismatch: bool = False,
) -> str:
    cfg = _cfg()
    j = get_job_with_fallback(task_id)
    if not j:
        return json.dumps({"error": "unknown task_id"})
    integrate_mode = mode or cfg.integrate_mode
    try:
        result = await _offload(git_ops.integrate, j, j["repo"], message, integrate_mode, allow_branch_mismatch)
    except Exception as e:  # noqa: BLE001 - never leak a raw traceback to the supervisor
        return json.dumps({
            "task_id": task_id, "integrated": False, "reason": "error",
            "detail": f"{type(e).__name__}: {e}",
        })
    return json.dumps(result)


@mcp.tool(
    description=(
        "Manages a multi-task batch. create(repo_path, goal, tasks_json) validates dependencies/"
        "scope overlap and returns parallel waves; status(batch_id) shows progress; finish(batch_id, "
        "verify_command?) integrates every approved task in dependency order and reports totals — "
        "repo_path is optional for status/finish (found from batch_id). tasks_json: JSON list of "
        "{key, title, dependsOn?, allowedFiles?}."
    )
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


# ── Configuration facade (§6.13, env-config-spec.md) ────────────────────
# There is nothing left to mutate here: every user-set value lives in an
# environment variable (config.py), read fresh on every call. `configure`
# is read-only except add_note/prune, which are repo housekeeping, not
# configuration.

_LIMIT_FIELDS = (
    "preflight_timeout_s", "verify_timeout_s", "wait_timeout_s", "wait_hard_cap_s",
    "max_diff_lines", "integrate_mode", "probe_ttl_s", "conventions_max_chars",
    "notes_max_chars", "max_budget_usd", "max_tokens_total", "timeout_s", "stall_s",
    "command_timeout_s", "ask_timeout_s", "recursion_limit_micro", "recursion_limit_task",
    "rubric_max_iterations_task",
)


def _probe_profile(resolved: dict[str, Any]) -> dict[str, Any]:
    return {"name": "default", "model": resolved["model"], "api_base": resolved["base_url"], "api_key": resolved["api_key"]}


@mcp.tool(
    description=(
        "Read-only environment and health checks; add_note/prune only on explicit request. "
        "status reports every MONKEY_* variable's resolved value or default (the API key shown "
        "only as set/not set, never its value) plus any validation errors. doctor runs a full "
        "health sweep. probe/discover_models check the configured endpoint directly."
    )
)
async def configure(
    action: str,
    repo_path: str | None = None,
    text: str | None = None,
    older_than_days: int | None = None,
) -> str:
    cfg = _cfg()
    if action == "status":
        resolved = config.required_config()
        return json.dumps({
            "home": str(config.home_dir()),
            "base_url": resolved.get("base_url"),
            "api_key_set": bool(resolved.get("api_key")),
            "model": resolved.get("model"),
            "fallback_models": resolved.get("fallback_models"),
            "prices": resolved.get("prices"),
            "model_kwargs": resolved.get("model_kwargs"),
            "limits": {field: getattr(cfg, field) for field in _LIMIT_FIELDS},
            "errors": resolved.get("errors", []),
            "last_probes": backend.all_last_probes(),
            "last_doctor_at": store.last_doctor_at(),
        })

    if action == "discover_models":
        resolved = config.required_config()
        if resolved["errors"]:
            return json.dumps({"error": "; ".join(resolved["errors"])})
        return json.dumps(await _offload(backend.discover_models, _probe_profile(resolved)))

    if action == "probe":
        resolved = config.required_config()
        if resolved["errors"]:
            return json.dumps({"error": "; ".join(resolved["errors"])})
        return json.dumps(await _offload(backend.probe, _probe_profile(resolved), ttl_s=cfg.probe_ttl_s))

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

    return json.dumps({"error": f"unknown action {action!r}"})


if __name__ == "__main__":
    mcp.run()
