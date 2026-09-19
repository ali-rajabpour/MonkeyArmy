"""Spawns the deepagents worker and folds its stdout into the job.

`uv run worker/worker.py ...` as a subprocess, consuming stdout line-by-line
as it arrives:

- ``PROGRESS:`` lines refresh job["progress"] + lastActivityTs (persisted so
  get_task_status sees them live) and feed the event bus (watch stream);
- ``QUESTION:`` lines flip the job to ``needs_input`` — the worker is then
  blocked waiting for answer_worker to drop a file in the comm dir;
- the final ``RESULT_JSON:`` line decides success/failure;
- a hard timeout (and cancel_task) kills the whole process TREE — killing
  only the direct child leaves grandchildren holding the stdout pipe, which
  is how a stuck command once froze a delegation for 20+ minutes;
- a shorter **stall watchdog** races the hard timeout: if the worker goes
  silent (no PROGRESS/QUESTION line) for longer than ``stall_timeout_s``
  (default 5 min), it's killed early instead of sitting until the full run
  budget expires. Catches a hung single model call — most often
  RubricMiddleware's post-loop grading step, which produces no stdout of its
  own while it waits on the provider. Exempts ``needs_input``, an
  intentional bounded wait for a supervisor answer;
- any non-succeeded ending triggers a salvage pass so completed-but-
  uncommitted work still reaches fetch_task_result.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from config import Config
from events import publish
from jobs import collect_diff, persist_job, runtime, salvage_worktree
from statusline_render import write_statusline
from persistence import (
    find_last_result_line,
    parse_progress_line,
    parse_question_line,
    progress_note,
    strip_result_marker,
)
from proc_utils import kill_tree

WORKER_SCRIPT = str(Path(__file__).resolve().parent.parent / "worker" / "worker.py")


def comm_dir_for(job: dict[str, Any], work_dir: str) -> Path:
    return Path(job["repo"]) / work_dir / "comm" / job["taskId"]


def _build_args(cfg: Config, args: dict[str, Any]) -> list[str]:
    # Model and key env var are resolved per task (config_store profile or
    # legacy env defaults) and passed in via `args` by main.py.
    cli = [
        "run", WORKER_SCRIPT,
        "--worktree", args["worktree"],
        "--spec", args["spec"],
        "--model", args.get("model") or cfg.model,
        "--api-key-env-var", args.get("api_key_env_var") or cfg.api_key_env_var,
        "--recursion-limit", str(args["recursion_limit"]),
        "--rubric-max-iterations", str(args["rubric_max_iterations"]),
        "--command-timeout", str(cfg.command_timeout_s),
    ]
    if args.get("definition_of_done"):
        cli += ["--definition-of-done", args["definition_of_done"]]
    if args.get("test_command"):
        cli += ["--test-command", args["test_command"]]
    if args.get("fallback_models"):
        cli += ["--fallback-models", ",".join(args["fallback_models"])]
    if args.get("max_budget_usd") is not None:
        cli += ["--max-budget-usd", str(args["max_budget_usd"])]
    return cli


async def run_worker(cfg: Config, job: dict[str, Any], args: dict[str, Any], timeout_ms: int) -> None:
    """Run one delegated task to completion, mutating + persisting `job`."""
    # A profile without a stored key (e.g. keyless local routing) runs
    # without MONKEY_WORKER_API_KEY set, so litellm/9Router falls back to
    # whatever default auth the endpoint expects.
    env = {**os.environ}
    resolved_key = args.get("api_key") or cfg.worker_api_key
    if resolved_key:
        env["MONKEY_WORKER_API_KEY"] = resolved_key
    else:
        env.pop("MONKEY_WORKER_API_KEY", None)

    # Mailbox for supervisor answers to worker questions (ask_supervisor /
    # report_blocker). The worker polls files here while blocked.
    comm_dir = comm_dir_for(job, cfg.work_dir)
    comm_dir.mkdir(parents=True, exist_ok=True)
    env["MONKEY_COMM_DIR"] = str(comm_dir)

    def _publish(event: dict[str, Any]) -> None:
        publish(job["repo"], job["taskId"], event, cfg.work_dir)

    def _touch(note: str | None = None) -> None:
        job["lastActivityTs"] = time.time()
        if note:
            job["progress"] = note
        persist_job(job, cfg.work_dir)
        write_statusline(job)  # refresh the token-free status line

    try:
        proc = await asyncio.create_subprocess_exec(
            "uv", *_build_args(cfg, args),
            # stdin MUST be detached: this server's own stdin is the MCP
            # protocol channel, and an inheriting child steals protocol bytes.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            start_new_session=(os.name != "nt"),
        )
    except FileNotFoundError:
        job["status"] = "failed"
        job["error"] = (
            "'uv' was not found on PATH. The worker needs uv to run worker/worker.py "
            "(https://docs.astral.sh/uv/getting-started/installation/)."
        )
        persist_job(job, cfg.work_dir)
        _publish({"kind": "failed", "error": job["error"]})
        return

    rt = runtime.setdefault(job["taskId"], {})
    rt["proc"] = proc
    job["workerPid"] = proc.pid
    job["startedAt"] = time.time()
    _touch("worker starting")
    _publish({"kind": "started", "model": args.get("model") or cfg.model, "pid": proc.pid})

    result_line: str | None = None
    tail_lines: list[str] = []

    async def consume() -> None:
        nonlocal result_line
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            tail_lines.append(line)
            if len(tail_lines) > 50:
                tail_lines.pop(0)
            if line.startswith("RESULT_JSON:"):
                result_line = line
                continue
            question = parse_question_line(line)
            if question:
                job["status"] = "needs_input"
                job["question"] = {**question, "askedAt": time.time()}
                _touch(f"worker asks: {question['message'][:120]}")
                _publish({"kind": question.get("kind", "question"), **question})
                continue
            progress = parse_progress_line(line)
            if progress:
                if progress.get("step"):
                    job["lastStep"] = progress["step"]
                _touch(progress_note(progress))
                _publish({"kind": progress.get("kind", "progress"), **progress})
        await proc.wait()

    def _finalize_failure(error: str, kind: str = "failed") -> None:
        # `kind` doubles as the terminal job status (except when cancelled),
        # so "timeout" actually reaches get_task_status/the status line
        # instead of always collapsing to "failed".
        job["status"] = "cancelled" if rt.get("cancelled") else kind
        job["error"] = "cancelled by supervisor" if rt.get("cancelled") else error
        job.pop("question", None)
        if salvage_worktree(cfg.work_dir, job):
            job["salvaged"] = True
        persist_job(job, cfg.work_dir)
        write_statusline(job)
        _publish({
            "kind": "cancelled" if rt.get("cancelled") else kind,
            "error": job["error"], "salvaged": job.get("salvaged", False),
        })

    async def _cancel_quietly(task: asyncio.Task) -> None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - best-effort teardown
            pass

    async def _watchdog() -> str:
        """Detects a worker gone silent — a hung single model call (most often
        RubricMiddleware's grading step, which runs after the main loop and
        emits no PROGRESS line of its own) leaves nothing in stdout to react
        to, so without this the run sits until the full `timeout_ms` budget
        expires. Exempts `needs_input`: that pause is an intentional, already
        bounded wait for a supervisor answer (up to MONKEY_ASK_TIMEOUT_S),
        not a hang.
        """
        while True:
            await asyncio.sleep(5)
            if job.get("status") == "needs_input":
                continue
            last = job.get("lastActivityTs") or job.get("startedAt") or time.time()
            idle = time.time() - last
            if idle > cfg.stall_timeout_s:
                return f"no activity for {int(idle)}s"

    consume_task = asyncio.create_task(consume())
    watchdog_task = asyncio.create_task(_watchdog())

    try:
        done, _pending = await asyncio.wait(
            {consume_task, watchdog_task},
            timeout=timeout_ms / 1000,
            return_when=asyncio.FIRST_COMPLETED,
        )
    except Exception as e:  # noqa: BLE001 - any launcher failure becomes a job failure
        await _cancel_quietly(consume_task)
        await _cancel_quietly(watchdog_task)
        kill_tree(proc.pid)
        _finalize_failure(f"{type(e).__name__}: {e}")
        return

    if consume_task in done:
        await _cancel_quietly(watchdog_task)
        try:
            consume_task.result()
        except Exception as e:  # noqa: BLE001 - consume() itself raised
            kill_tree(proc.pid)
            _finalize_failure(f"{type(e).__name__}: {e}")
            return
        # Fell through: consume() finished normally — proceed to result parsing below.
    elif watchdog_task in done:
        await _cancel_quietly(consume_task)
        reason = watchdog_task.result()
        kill_tree(proc.pid)
        _finalize_failure(
            f"worker stalled: {reason} (likely a hung model call, e.g. rubric grading) — "
            f"killed after the {cfg.stall_timeout_s}s stall timeout instead of waiting the "
            f"full {timeout_ms}ms run timeout",
            kind="timeout",
        )
        return
    else:
        # Neither finished within the overall run budget — the pre-existing hard cap.
        await _cancel_quietly(consume_task)
        await _cancel_quietly(watchdog_task)
        kill_tree(proc.pid)
        _finalize_failure(f"worker timed out after {timeout_ms} ms", kind="timeout")
        return

    final_line = result_line or find_last_result_line("\n".join(tail_lines))
    if not final_line:
        _finalize_failure(
            "worker produced no result line; last stdout: " + "\n".join(tail_lines[-10:])
        )
        return

    try:
        result = json.loads(strip_result_marker(final_line))
    except (json.JSONDecodeError, ValueError) as e:
        _finalize_failure(f"unparseable RESULT_JSON line: {e}")
        return

    job["turns"] = result.get("turns", 0)
    job["summary"] = result.get("summary")
    job["costUsd"] = result.get("cost_usd")
    job["totalTokens"] = result.get("total_tokens")
    job.pop("question", None)

    if result.get("status") == "succeeded" and not rt.get("cancelled"):
        diff = collect_diff(cfg.work_dir, job["repo"], job["worktree"], job["taskId"])
        job.update(diff)
        job["status"] = "succeeded"
        persist_job(job, cfg.work_dir)
        write_statusline(job)
        _publish({
            "kind": "succeeded",
            "files_changed": len(job.get("filesChanged", [])),
            "cost_usd": job.get("costUsd"),
        })
    else:
        _finalize_failure(result.get("error") or "worker reported failure")
