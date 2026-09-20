"""Spawns the deepagents worker and folds its stdout into the job.

`uv run worker/worker.py ...` as a subprocess, consuming stdout line-by-line
as it arrives:

- ``PROGRESS:`` lines refresh job["progress"] + lastActivityTs (persisted so
  task_status sees them live) and feed the event bus (watch stream);
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
  uncommitted work still reaches task_result.

Before spawning, this module also assembles the worker's brief file (§8.2)
and the spawn environment: the env is filtered of anything that looks like a
secret (§7.5), then a git-remote neutralisation block is added so nothing the
worker runs can push, fetch, or prompt for credentials (I3), belt-and-braces
alongside the worker's own git command allowlist.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import store
import verify
from config import Defaults
from events import publish
from jobs import changed_files, persist_job, runtime, salvage_worktree
from statusline_render import write_statusline
from persistence import (
    find_last_result_line,
    parse_progress_line,
    parse_question_line,
    progress_note,
    repo_state_dir,
    strip_result_marker,
)
from proc_utils import kill_tree

WORKER_SCRIPT = str(Path(__file__).resolve().parent.parent / "worker" / "worker.py")

# Kept as an intentional duplicate of worker/worker.py's copy (§7.5): the
# launcher filters the *process spawn* env, the worker filters the *shell
# tool* env — two different trust boundaries, so one shared import would
# blur which side actually enforces what. test_launcher_env.py asserts the
# two copies agree by parsing worker.py's source (it can't be imported here
# without its third-party deps).
_SENSITIVE_ENV_SUBSTRINGS: tuple[str, ...] = (
    "API_KEY",
    "APIKEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
)


def is_sensitive_env_name(name: str) -> bool:
    upper = name.upper()
    return any(token in upper for token in _SENSITIVE_ENV_SUBSTRINGS)


def comm_dir_for(job: dict[str, Any]) -> Path:
    return repo_state_dir(job["repo"]) / "comm" / job["taskId"]


def _git_remotes(repo: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "remote"], cwd=repo, capture_output=True, text=True,
            check=True, stdin=subprocess.DEVNULL, timeout=10,
        ).stdout
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _git_neutralization_env(repo: str) -> dict[str, str]:
    """§7.5 GIT_CONFIG_* block. Because the worker passes its (filtered)
    process env straight through to the shell backend, this reaches every
    `git` invocation the worker makes, whatever command form it uses —
    unlike the tool-layer allowlist (§8.5), which only sees commands issued
    through the shell tool.
    """
    # protocol.allow=never is the real stop: env-injected remote.<n>.url only
    # ADDS a second url (fetch still uses the repo's own first one), and an
    # explicit path/URL (`git push ../x.git`) bypasses remotes entirely.
    # Verified: push, fetch, pull, ls-remote and clone all fail with
    # "transport '<x>' not allowed". The url/pushurl entries stay as a
    # readable marker in `git remote -v`.
    pairs: list[tuple[str, str]] = [
        ("protocol.allow", "never"),
        ("credential.helper", ""),
        ("core.askPass", "false" if os.name == "nt" else "/usr/bin/false"),
    ]
    for remote in _git_remotes(repo):
        pairs.append((f"remote.{remote}.pushurl", "monkey-army-blocked://push-disabled"))
        pairs.append((f"remote.{remote}.url", "monkey-army-blocked://fetch-disabled"))
    env = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": str(len(pairs))}
    for i, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


def build_spawn_env(job: dict[str, Any], args: dict[str, Any], comm_dir: Path, ask_timeout_s: int) -> dict[str, str]:
    # SSH_AUTH_SOCK would let `git push <explicit ssh url>` bypass the
    # remote.<name>.url overrides below; stale GIT_CONFIG_* indices from the
    # parent env would survive our block.
    env = {
        k: v for k, v in os.environ.items()
        if not is_sensitive_env_name(k) and k != "SSH_AUTH_SOCK" and not k.startswith("GIT_CONFIG_")
    }
    env["GIT_SSH_COMMAND"] = "false"
    resolved_key = args.get("api_key")
    if resolved_key:
        env["MONKEY_WORKER_API_KEY"] = resolved_key
    else:
        env.pop("MONKEY_WORKER_API_KEY", None)
    env["MONKEY_COMM_DIR"] = str(comm_dir)
    env["MONKEY_ASK_TIMEOUT_S"] = str(ask_timeout_s)
    env.update(_git_neutralization_env(job["repo"]))
    return env


def _read_conventions(repo: str, max_chars: int) -> str:
    for name in ("AGENTS.md", "CLAUDE.md"):
        try:
            return (Path(repo) / name).read_text(encoding="utf-8")[:max_chars]
        except (FileNotFoundError, OSError):
            continue
    return ""


def build_brief(
    job: dict[str, Any], args: dict[str, Any], defaults: Defaults,
    feedback_history: list[dict[str, Any]] | None = None,
) -> Path:
    """Write comm/<id>/brief.md (§8.2) and return its path. Rebuilt (with a
    feedback section appended) on every retry — the worker always reads the
    brief file fresh, so this is the only thing that needs to change between
    attempts in the same worktree.
    """
    allowed = args.get("allowed_files") or []
    allowed_section = "\n".join(f"- {g}" for g in allowed) if allowed else "- unrestricted — but stay minimal"
    context = args.get("context_files") or []
    context_section = "\n".join(f"- {c}" for c in context) if context else "- (none given)"
    test_command = args.get("test_command")
    dod = args.get("definition_of_done") or (
        f"Running `{test_command}` exits 0." if test_command else "Make the described change."
    )
    acceptance = test_command or "(no acceptance command given — use your own judgement)"
    conventions = _read_conventions(job["repo"], defaults.conventions_max_chars)
    notes = store.read_notes(job["repo"], defaults.notes_max_chars)

    parts = [
        f"# Task: {args['title']}",
        args["spec"],
        "",
        "# Allowed files (you may create/modify ONLY these; anything else fails the task)",
        allowed_section,
        "",
        "# Read first",
        context_section,
        "",
        "# Definition of done",
        dod,
        "",
        "# Acceptance command",
        f"`{acceptance}`   ← run it; iterate until it exits 0; then stop.",
    ]
    if conventions:
        parts += ["", "# Repository conventions (excerpt of AGENTS.md / CLAUDE.md)", conventions]
    if notes:
        parts += ["", "# Supervisor notes for this repository", notes]
    if feedback_history:
        # One section PER attempt, not just the latest — a worker on attempt
        # 3 needs to see what it was told after attempts 1 AND 2.
        for entry in feedback_history:
            parts += ["", f"# Supervisor feedback on attempt {entry['attempt']}", entry["feedback"]]
        parts += ["Your previous changes are present in the working directory. Fix them; do not start over."]

    comm_dir = comm_dir_for(job)
    comm_dir.mkdir(parents=True, exist_ok=True)
    brief_path = comm_dir / "brief.md"
    brief_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return brief_path


def build_worker_args(job: dict[str, Any], cfg: Defaults, resolved: dict[str, Any]) -> dict[str, Any]:
    """The `args` dict `run_worker`/`retry` need, built from a job's own
    persisted fields plus a freshly `config.required_config()`d env config.

    Shared by dispatch_task (first attempt) and review_task's reject path
    (retry): re-resolving fresh — rather than keeping the launch args around
    on the job — means a retry works even across a server restart, and never
    needs to persist secrets on the job. Limits come straight from `cfg`
    (env-only, §env-config-spec.md) — there is no per-profile override to
    merge anymore.
    """
    mode = job.get("mode", "micro")
    recursion_default = cfg.recursion_limit_micro if mode == "micro" else cfg.recursion_limit_task
    prices = resolved.get("prices") or {}
    return {
        "title": job.get("title"), "spec": job.get("spec"), "worktree": job["worktree"],
        "test_command": job.get("testCommand"), "definition_of_done": job.get("definitionOfDone"),
        "allowed_files": job.get("allowedFiles") or [], "context_files": job.get("contextFiles") or [],
        "mode": mode, "model": resolved["model"], "api_base": resolved.get("base_url"),
        "api_key_env_var": "MONKEY_9ROUTER_KEY", "api_key": resolved.get("api_key"),
        "fallback_models": resolved.get("fallback_models") or [],
        "model_kwargs": resolved.get("model_kwargs") or {},
        "price_in": prices.get("input"), "price_out": prices.get("output"),
        "max_budget_usd": job.get("maxBudgetUsd") or cfg.max_budget_usd,
        "max_tokens_total": job.get("maxTokensTotal") or cfg.max_tokens_total,
        "recursion_limit": recursion_default,
        "rubric_max_iterations": cfg.rubric_max_iterations_task,
        "command_timeout": cfg.command_timeout_s,
        "ask_timeout_s": cfg.ask_timeout_s,
    }


def _fmt_opt(value: Any) -> str:
    """CLI convention worker.py expects: empty string means "unset" (§8.2)."""
    return "" if value is None else str(value)


def _build_args(cfg: Defaults, args: dict[str, Any], brief_path: Path) -> list[str]:
    return [
        "run", WORKER_SCRIPT,
        "--worktree", args["worktree"],
        "--brief", str(brief_path),
        "--model", args["model"],
        "--api-base", _fmt_opt(args.get("api_base")),
        "--api-key-env-var", args.get("api_key_env_var") or "",
        "--fallback-models", ",".join(args.get("fallback_models") or []),
        "--model-kwargs-json", json.dumps(args["model_kwargs"]) if args.get("model_kwargs") else "",
        "--price-in", _fmt_opt(args.get("price_in")),
        "--price-out", _fmt_opt(args.get("price_out")),
        "--max-budget-usd", _fmt_opt(args.get("max_budget_usd")),
        "--max-tokens-total", _fmt_opt(args.get("max_tokens_total")),
        "--mode", args.get("mode") or "micro",
        "--recursion-limit", _fmt_opt(args.get("recursion_limit")),
        "--rubric-max-iterations", str(args.get("rubric_max_iterations", 4)),
        "--command-timeout", str(args.get("command_timeout") or cfg.command_timeout_s),
        "--allowed-files", ",".join(args.get("allowed_files") or []),
        "--test-command", args.get("test_command") or "",
        "--definition-of-done", args.get("definition_of_done") or "",
    ]


async def run_worker(
    cfg: Defaults, job: dict[str, Any], args: dict[str, Any], timeout_ms: int,
    feedback_history: list[dict[str, Any]] | None = None,
) -> None:
    """Run one delegated task to completion, mutating + persisting `job`."""
    comm_dir = comm_dir_for(job)
    comm_dir.mkdir(parents=True, exist_ok=True)
    brief_path = build_brief(job, args, cfg, feedback_history=feedback_history)

    ask_timeout_s = args.get("ask_timeout_s") or cfg.ask_timeout_s
    env = build_spawn_env(job, args, comm_dir, ask_timeout_s)

    def _publish(event: dict[str, Any]) -> None:
        publish(job["repo"], job["taskId"], event)

    def _touch(note: str | None = None) -> None:
        job["lastActivityTs"] = time.time()
        if note:
            job["progress"] = note
        persist_job(job)
        write_statusline(job)  # refresh the token-free status line

    try:
        proc = await asyncio.create_subprocess_exec(
            "uv", *_build_args(cfg, args, brief_path),
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
        persist_job(job)
        _publish({"kind": "failed", "error": job["error"]})
        return

    rt = runtime.setdefault(job["taskId"], {})
    rt["proc"] = proc
    job["workerPid"] = proc.pid
    job["startedAt"] = time.time()
    _touch("worker starting")
    _publish({"kind": "started", "model": args["model"], "pid": proc.pid})

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
        # so "timeout" actually reaches task_status/the status line
        # instead of always collapsing to "failed".
        job["status"] = "cancelled" if rt.get("cancelled") else kind
        job["error"] = "cancelled by supervisor" if rt.get("cancelled") else error
        job["finishedAt"] = time.time()
        job.pop("question", None)
        if salvage_worktree(job):
            job["salvaged"] = True
        persist_job(job)
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
            if idle > cfg.stall_s:
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
            f"killed after the {cfg.stall_s}s stall timeout instead of waiting the "
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
    job["priced"] = result.get("priced", False)
    job["modelsSeen"] = result.get("models_seen", [])
    # The worker's own verdict, kept for the record — it does NOT decide
    # job.status on its own (I4): §7.3's finalize_success re-verifies, scope-
    # checks, caps the diff, and commits. The one exception is a worker
    # failure that left nothing changed — nothing for the pipeline to do.
    job["workerClaimedStatus"] = result.get("status")
    job.pop("question", None)

    if rt.get("cancelled"):
        # A cancel raced the worker's own RESULT_JSON — cancellation wins
        # regardless of what the worker claims; today's salvage path applies.
        _finalize_failure(result.get("error") or "worker reported failure")
        return

    if result.get("status") != "succeeded" and not changed_files(job["worktree"]):
        job["status"] = "failed"
        job["error"] = result.get("error") or "worker reported failure"
        job["finishedAt"] = time.time()
        persist_job(job)
        write_statusline(job)
        _publish({"kind": "failed", "error": job["error"]})
        return

    # Either the worker claimed success, or it claimed failure but left
    # changes on disk (the work may be complete — a field lesson, §7.3): both
    # go through the same server-side pipeline, which decides the real
    # verdict. It runs real git/test subprocesses, so keep it off the event
    # loop.
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, verify.finalize_success, job, cfg)


async def retry(
    cfg: Defaults, job: dict[str, Any], args: dict[str, Any],
    feedback_history: list[dict[str, Any]], timeout_ms: int,
) -> None:
    """Re-spawn the worker in the SAME worktree with the brief extended by
    one "Supervisor feedback on attempt N" section per past attempt — used
    by review_task(verdict='reject'). Caller is responsible for having
    already bumped job["attempt"], appended to job["feedbackHistory"], and
    set job["status"] = "running" before awaiting this.
    """
    job.pop("question", None)
    await run_worker(cfg, job, args, timeout_ms, feedback_history=feedback_history)
