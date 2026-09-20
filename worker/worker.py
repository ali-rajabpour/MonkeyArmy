# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "deepagents==0.7.15",
#   "langchain-litellm==0.7.2",
#   "litellm==1.101.0",
#   "fastapi==0.116.1",
# ]
# ///
"""Runs one delegated coding task with deepagents, then prints a single
JSON result line to stdout. Invoked as a subprocess by the MCP server
(server/worker_launcher.py) — this script owns the agent loop only; git
worktree setup, diff collection, and job bookkeeping stay in the server.

Stdout is the upward channel to the server:

- ``PROGRESS:`` lines — one per graph step, per shell command start, and per
  explicit report_progress call — feed get_task_status and the live dashboard;
- ``QUESTION:`` lines — emitted by ask_supervisor / report_blocker — flip the
  job to ``needs_input``; the worker then blocks (token-free) polling the
  comm dir until answer_worker drops a reply file;
- the final ``RESULT_JSON:`` line carries the verdict.

``--selftest`` skips all of the above: it just imports the heavy deps,
constructs a model and a backend with dummy values (no network), and prints
``SELFTEST_OK`` — used by the server's doctor to warm the uv cache and catch
a broken install before a real task ever starts.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

import litellm
from langchain_litellm import ChatLiteLLM

from deepagents import RubricMiddleware, SubAgent, create_deep_agent
from deepagents.backends.local_shell import LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse

RESULT_MARKER = "RESULT_JSON:"
PROGRESS_MARKER = "PROGRESS:"
QUESTION_MARKER = "QUESTION:"

DEFAULT_COMMAND_TIMEOUT = 120
DEFAULT_ASK_TIMEOUT = 600
DEFAULT_RECURSION_LIMIT_MICRO = 80
DEFAULT_RECURSION_LIMIT_TASK = 400

# Whole-drive scans (`find /`, `find C:/`) once froze a delegation for 20+
# minutes. The per-command timeout now bounds the damage, but there is never
# a reason to leave the worktree — refuse outright and tell the model why.
_DRIVE_SCAN_RE = re.compile(r"""\bfind\s+['"]?(?:/|[A-Za-z]:[/\\]?)['"]?(?:\s|$)""")


# ── Git allowlist (tool-level enforcement) ──────────────────────────────────
# The system prompt already tells the worker never to push/merge/rebase —
# git_command_allowed enforces that at the tool layer instead of trusting the
# model's word, since a worker only ever operates on its own disposable
# branch and there is never a legitimate reason for it to touch shared
# history. Default is deny: only an explicit allowlist of inspection/local
# git subcommands passes; everything else, known or not, is blocked.
_GIT_SEPARATORS = {"&&", "||", ";", "|"}
_GIT_GLOBAL_OPTS_NO_ARG = {"--no-pager", "-p", "--paginate"}
# `-c`/`-C` outrank GIT_CONFIG_*, and --git-dir/--work-tree/--exec-path/
# --namespace let a worker point git at an entirely different repo — none of
# that is stoppable from the environment layer, so these are blocked
# outright rather than skipped-over like the read-only options above.
_GIT_GLOBAL_OPTS_BLOCKED_WITH_ARG = {"-c", "-C"}
_GIT_GLOBAL_OPTS_BLOCKED_PREFIXED = ("--git-dir", "--work-tree", "--exec-path", "--namespace")

_GIT_ALLOWED_SUBCOMMANDS = {
    "status", "diff", "log", "show", "add", "blame", "grep", "ls-files",
    "rev-parse", "apply", "rm", "mv", "restore", "merge-base",
}
_GIT_BRANCH_SAFE_ARGS = {"--show-current", "--list", "-a"}

_GIT_BLOCKED_MSG = (
    "git {sub} is blocked for workers: you operate on a disposable branch; the supervisor "
    "reviews and merges."
)


def _tokenize_shell(command: str) -> list[str] | None:
    """POSIX-ish tokenizer that also splits ``&&``/``||``/``;``/``|`` when
    glued to a neighbouring word.

    Plain ``shlex.split`` leaves ``git status;git push`` as a single token
    ``"status;git"`` (no whitespace around the ``;``), which would hide the
    chained ``git push`` from the allowlist check below. ``shlex.shlex`` with
    ``punctuation_chars=True`` treats those operator characters as their own
    tokens even without surrounding whitespace, while still respecting
    quoting (``sh -c "git push"`` stays a single quoted token).
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _split_on_separators(tokens: list[str]) -> list[list[str]]:
    commands: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _GIT_SEPARATORS:
            if current:
                commands.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        commands.append(current)
    return commands


def _git_blocked_global_option(tokens: list[str]) -> str | None:
    """First blocked global option token found before the subcommand, or
    None. Covers both the inline (``--git-dir=x``) and two-token
    (``--git-dir x``) forms git itself accepts for the prefixed options."""
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in _GIT_GLOBAL_OPTS_BLOCKED_WITH_ARG:
            return tok
        if tok.startswith(_GIT_GLOBAL_OPTS_BLOCKED_PREFIXED):
            return tok.split("=", 1)[0]
        if tok in _GIT_GLOBAL_OPTS_NO_ARG:
            i += 1
            continue
        return None
    return None


def _git_subcommand_index(tokens: list[str]) -> int | None:
    """Index of the first token after ``git`` that isn't an allowed
    (read-only) global option. Caller checks ``_git_blocked_global_option``
    first — anything else that looks like a flag here just becomes the
    "subcommand" candidate and fails the allowlist (default deny)."""
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in _GIT_GLOBAL_OPTS_NO_ARG:
            i += 1
        else:
            return i
    return None


def _git_subcommand_allowed(sub: str, rest: list[str]) -> bool:
    if sub == "branch":
        # No args (plain listing) or only read-only flags — never a rename/delete.
        return all(tok in _GIT_BRANCH_SAFE_ARGS for tok in rest)
    return sub in _GIT_ALLOWED_SUBCOMMANDS


def git_command_allowed(command: str) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for a shell command that may invoke git.

    Every ``git`` invocation anywhere in the command line — including chained
    via ``&&``/``||``/``;``/``|`` and nested inside ``sh -c "..."``/``bash -c
    "..."`` — must resolve to an allowlisted subcommand. Non-git commands
    always pass. A command that can't be tokenized is blocked outright: we
    can't rule out a hidden git call inside it.
    """
    tokens = _tokenize_shell(command)
    if tokens is None:
        return False, "could not parse command"

    for simple in _split_on_separators(tokens):
        if not simple:
            continue
        # Any quoted/substituted argument that itself contains a git call
        # (`sh -lc "git push"`, `echo $(git push)`, `xargs sh -c '...'`) is
        # checked as its own command line.
        for arg in simple[1:]:
            if "git" in arg and any(c.isspace() or c in "$`;|&" for c in arg):
                ok, msg = git_command_allowed(arg.replace("$(", " ").replace("`", " ").replace(")", " "))
                if not ok:
                    return ok, msg

        # Wrappers (`env git`, `xargs git`, `nohup git`, `command git`) put
        # git after the first token; start from the first git token instead.
        git_at = next(
            (i for i, t in enumerate(simple) if t == "git" or t.endswith("/git")), None
        )
        if git_at is None:
            continue
        simple = simple[git_at:]

        blocked_opt = _git_blocked_global_option(simple)
        if blocked_opt is not None:
            return False, f"git global option {blocked_opt} is blocked for workers"

        idx = _git_subcommand_index(simple)
        if idx is None:
            continue
        sub = simple[idx]
        if not _git_subcommand_allowed(sub, simple[idx + 1:]):
            return False, _GIT_BLOCKED_MSG.format(sub=sub)

    return True, ""


def emit_progress(payload: dict) -> None:
    print(PROGRESS_MARKER + json.dumps(payload), flush=True)


def emit_question(payload: dict) -> None:
    print(QUESTION_MARKER + json.dumps(payload), flush=True)


def kill_tree(pid: int) -> None:
    """Kill ``pid`` and every descendant — a plain kill leaves grandchildren
    holding the output pipes, which turns one stuck command into a stuck run."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, stdin=subprocess.DEVNULL, timeout=15,
            )
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001 - cleanup must never crash the agent loop
        pass


def _find_bash() -> str | None:
    """Locate a bash executable on Windows so the worker's shell commands run
    under bash instead of cmd.exe.

    Models routinely emit Unix commands (``ls``, ``find``, ``cat``, ``&&``,
    forward-slash paths) that cmd.exe rejects — the worker then burns turns
    fighting the shell. Routing through bash (present via Git/hermes on most
    dev machines) fixes that. Returns None when no bash is found, in which
    case commands run through the default shell.
    """
    found = shutil.which("bash")
    if found:
        return found
    for cand in (
        os.path.expandvars(r"%LOCALAPPDATA%\hermes\git\usr\bin\bash.exe"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ):
        if os.path.isfile(cand):
            return cand
    return None


class SupervisedShellBackend(LocalShellBackend):
    """LocalShellBackend with field-tested hardenings:

    1. **Command-start announcements** — every command is echoed as a
       ``PROGRESS:`` line before it runs, so the dashboard and
       get_task_status show live activity instead of a frozen marker.
    2. **Tree-killing timeout** — the stock backend's
       ``subprocess.run(shell=True, timeout=...)`` kills only the direct
       shell on timeout; on Windows CPython then re-``communicate()``s
       without a timeout, and a surviving grandchild holding the stdout
       pipe hangs the whole run indefinitely. We run the process ourselves
       and kill the entire tree.
    3. **bash routing on Windows** — each command is written to a temp
       script and run via ``[bash, script]`` (no cmd.exe, no quoting
       conflicts). Elsewhere the default shell is already sh-compatible.
    4. **Git allowlist** — every command is checked with
       ``git_command_allowed`` before it runs.
    """

    def __init__(self, *args, bash_path: str | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._bash_path = bash_path

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if not command or not isinstance(command, str):
            return ExecuteResponse(
                output="Error: Command must be a non-empty string.", exit_code=1, truncated=False
            )
        if _DRIVE_SCAN_RE.search(command):
            return ExecuteResponse(
                output=(
                    "Error: refusing to scan a filesystem/drive root. Everything relevant is "
                    "inside the current working directory — search there instead "
                    "(e.g. `find . -name ...` or `ls <subdir>`)."
                ),
                exit_code=1,
                truncated=False,
            )
        allowed, reason = git_command_allowed(command)
        if not allowed:
            return ExecuteResponse(output=f"Error: {reason}", exit_code=1, truncated=False)

        effective_timeout = timeout if timeout is not None else self._default_timeout
        emit_progress({
            "kind": "shell",
            "command": command[:200],
            "note": "$ " + command[:160],
        })

        script_path: str | None = None
        try:
            if self._bash_path:
                fd, script_path = tempfile.mkstemp(suffix=".sh", dir=str(self.cwd))
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                    f.write("set -e\n" + command + "\n")
                popen_args = [self._bash_path, script_path]
                shell = False
            else:
                popen_args = command
                shell = True
            proc = subprocess.Popen(
                popen_args,
                shell=shell,
                cwd=str(self.cwd),
                env=self._env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=(os.name != "nt"),
            )
            try:
                stdout, stderr = proc.communicate(timeout=effective_timeout)
            except subprocess.TimeoutExpired:
                kill_tree(proc.pid)
                try:
                    stdout, stderr = proc.communicate(timeout=10)
                except Exception:  # noqa: BLE001
                    stdout, stderr = "", ""
                partial = (stdout or "")[-2000:]
                msg = (
                    f"Error: Command timed out after {effective_timeout} seconds and its whole "
                    "process tree was killed. Do not simply retry it — use a narrower/faster "
                    "variant, or pass a larger `timeout` only if the command legitimately needs it."
                )
                if partial.strip():
                    msg += f"\n\nPartial output before the kill:\n{partial}"
                return ExecuteResponse(output=_append_steer_notice(msg), exit_code=124, truncated=False)
        except Exception as e:  # noqa: BLE001 - mirror base: errors become a response
            return ExecuteResponse(
                output=f"Error executing command ({type(e).__name__}): {e}",
                exit_code=1,
                truncated=False,
            )
        finally:
            if script_path:
                try:
                    os.remove(script_path)
                except OSError:
                    pass

        # Same output shaping as the stock backend: stderr lines prefixed,
        # size cap, exit-code note.
        output_parts = []
        if stdout:
            output_parts.append(stdout)
        if stderr:
            output_parts.extend(f"[stderr] {line}" for line in stderr.strip().split("\n"))
        output = "\n".join(output_parts) if output_parts else "<no output>"

        truncated = False
        if len(output) > self._max_output_bytes:
            output = output[: self._max_output_bytes]
            output += f"\n\n... Output truncated at {self._max_output_bytes} bytes."
            truncated = True
        if proc.returncode != 0:
            output = f"{output.rstrip()}\n\nExit code: {proc.returncode}"

        return ExecuteResponse(
            output=_append_steer_notice(output), exit_code=proc.returncode, truncated=truncated
        )


# ── Supervisor communication tools ──────────────────────────────────────────
# Exposed to the agent so it can talk UPWARD during the run instead of only
# delivering a final report. report_progress is fire-and-forget; the other
# two block this process (zero tokens burned) until the supervisor answers
# through the answer_worker MCP tool, which drops a file in MONKEY_COMM_DIR.


def _ask_blocking(kind: str, message: str, context: str) -> str:
    comm_dir = os.environ.get("MONKEY_COMM_DIR")
    if not comm_dir:
        return (
            "Supervisor channel unavailable in this run. Proceed autonomously with the most "
            "conservative reasonable choice and record the open question in your final summary."
        )
    qid = uuid.uuid4().hex[:12]
    emit_question({"id": qid, "kind": kind, "message": message[:2000], "context": context[:2000]})

    answer_path = os.path.join(comm_dir, f"{qid}.json")
    timeout_s = int(os.environ.get("MONKEY_ASK_TIMEOUT_S", str(DEFAULT_ASK_TIMEOUT)))
    waited = 0.0
    while waited < timeout_s:
        if os.path.isfile(answer_path):
            try:
                with open(answer_path, encoding="utf-8") as f:
                    payload = json.load(f)
                answer = payload.get("answer")
            except (OSError, json.JSONDecodeError, ValueError):
                answer = None
            try:
                os.remove(answer_path)
            except OSError:
                pass
            if isinstance(answer, str) and answer:
                emit_progress({"kind": "report", "note": "supervisor answered; resuming"})
                return f"Supervisor answered: {answer}"
        time.sleep(2)
        waited += 2
        if int(waited) % 30 == 0:
            emit_progress({"kind": "waiting", "note": f"waiting for supervisor answer ({int(waited)}s)"})

    emit_progress({"kind": "report", "note": "no supervisor answer; proceeding autonomously"})
    return (
        f"No supervisor answer within {timeout_s}s. Proceed with your best judgment: prefer the "
        "most conservative choice, and record the open question in your final summary."
    )


def check_steer_message() -> str | None:
    """Read-and-clear a pending supervisor steer message, or None.

    Unlike ask_supervisor/report_blocker this never blocks — the supervisor
    can push guidance at any moment via the steer_task MCP tool, and it just
    sits in the comm dir until the worker's next tool call opportunistically
    picks it up (there is no way to interrupt an in-flight LangGraph step
    from outside, so "at any moment" really means "within one tool call").
    """
    comm_dir = os.environ.get("MONKEY_COMM_DIR")
    if not comm_dir:
        return None
    path = os.path.join(comm_dir, "steer.json")
    if not os.path.isfile(path):
        return None
    message = None
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        message = payload.get("message")
    except (OSError, json.JSONDecodeError, ValueError):
        message = None
    try:
        os.remove(path)
    except OSError:
        pass
    return message if isinstance(message, str) and message else None


def _append_steer_notice(text: str) -> str:
    message = check_steer_message()
    if not message:
        return text
    return f"{text}\n\n⚠ SUPERVISOR STEERING (act on this now): {message}"


def report_progress(update: str) -> str:
    """Send a one-line progress update to the supervisor and the user's live dashboard.

    Call this at every phase transition (starting implementation, tests
    passing, refactoring, ...) and after completing each significant file.
    It is fire-and-forget: execution continues immediately.

    Args:
        update: one short sentence, e.g. "implemented src/auth/tokens.js, moving to tests".
    """
    emit_progress({"kind": "report", "note": str(update)[:300]})
    return _append_steer_notice("progress update delivered")


def ask_supervisor(question: str, context: str = "") -> str:
    """Ask the supervising agent a question and WAIT for its answer.

    Use when the spec is ambiguous, two valid designs conflict, or a decision
    belongs to the user (naming, API shape, dependency choice, destructive
    change). Execution pauses until the supervisor replies (or a timeout
    passes) — so batch related doubts into one question and keep working on
    independent parts afterwards.

    Args:
        question: the decision you need, phrased so a yes/no or short answer unblocks you.
        context: what you tried / the options you weighed, so the supervisor can decide fast.
    """
    return _ask_blocking("question", str(question), str(context))


def report_blocker(problem: str, attempts: str = "") -> str:
    """Report a blocker to the supervisor and WAIT for guidance.

    Use after roughly three failed attempts at the SAME error (test that
    won't pass, command that keeps failing, missing dependency) instead of
    burning more attempts on it. The supervisor sees your problem and
    attempts, and replies with guidance, a corrected command, or a decision.

    Args:
        problem: the exact error/blocker, with the key error line verbatim.
        attempts: what you already tried, so the supervisor does not suggest it again.
    """
    return _ask_blocking("blocker", str(problem), str(attempts))


# Substrings (case-insensitive) that mark an env var as a secret. Matched
# against the uppercased name with ``in`` — so ``MY_API_KEY``,
# ``GITHUB_TOKEN``, ``DB_PASSWORD`` are all filtered. ``is_sensitive_env_name``
# is a pure function so it stays unit-testable without touching ``os.environ``.
_SENSITIVE_ENV_SUBSTRINGS: tuple[str, ...] = (
    "API_KEY",
    "APIKEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
)


def is_sensitive_env_name(name: str) -> bool:
    """Return True if ``name`` looks like a secret-bearing environment variable.

    The check is intentionally broad: any substring hit (case-insensitive) on
    API_KEY / APIKEY / TOKEN / SECRET / PASSWORD / CREDENTIAL qualifies. We
    err on the side of dropping more variables rather than risking a leak
    through a shell command the agent runs. PATH / HOME / SystemRoot / TEMP
    are unaffected, and so are GIT_CONFIG_* / GIT_TERMINAL_PROMPT — those
    carry the launcher's remote-neutralisation block and must reach `git`.
    """
    upper = name.upper()
    return any(token in upper for token in _SENSITIVE_ENV_SUBSTRINGS)


def build_shell_env(api_key_env_var: str) -> dict[str, str]:
    """Return a copy of ``os.environ`` safe to hand to the shell backend.

    Drops anything that matches ``is_sensitive_env_name``, plus the well-known
    secret names ``MONKEY_WORKER_API_KEY`` and the provider-specific key env
    var named by ``api_key_env_var``. PATH, HOME, SystemRoot, TEMP, and
    similar survive so that ``git``, ``node``, ``npm``, etc. keep working
    inside the worktree — as do ``GIT_CONFIG_*``/``GIT_TERMINAL_PROMPT``, which
    the launcher sets to neutralise remotes and are not secrets.

    IMPORTANT: this only filters the dict we hand to the shell backend — the
    Python process's own ``os.environ`` keeps the provider key, because
    litellm reads it from there.
    """
    drop_names = {"MONKEY_WORKER_API_KEY", api_key_env_var}
    return {
        k: v
        for k, v in os.environ.items()
        if k not in drop_names and not is_sensitive_env_name(k)
    }


def _usage_value(usage, name: str):
    if hasattr(usage, name):
        return getattr(usage, name)
    if isinstance(usage, dict):
        return usage.get(name)
    return None


class CostTracker:
    """litellm success callback that accumulates cost + tokens across every
    model call in the run (main agent, subagents, rubric grader).

    litellm has no pricing entry for most 9Router ``combo/*`` ids, so on top
    of ``litellm.completion_cost`` this falls back to the profile's flat
    per-token prices (``price_in``/``price_out``, USD per 1M tokens) whenever
    a call comes back unpriced — the budget loop and RESULT_JSON then still
    report a real number instead of silently staying at zero.
    """

    def __init__(self, price_in: float | None, price_out: float | None,
                 max_tokens_total: int | None) -> None:
        self.price_in = price_in
        self.price_out = price_out
        self.max_tokens_total = max_tokens_total
        self.cost_usd: float = 0.0
        self.total_tokens: int = 0
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self._priced_calls: int = 0
        self._models_seen: dict[str, None] = {}  # insertion-ordered set

    def __call__(self, kwargs, completion_response, start_time, end_time) -> None:  # noqa: ARG002
        priced_this_call = False
        try:
            cost = litellm.completion_cost(completion_response=completion_response)
            if cost is not None and isinstance(cost, (int, float)) and float(cost) >= 0:
                self.cost_usd += float(cost)
                priced_this_call = True
        except Exception:
            # Unknown model / missing pricing entry — try the fallback below.
            pass

        prompt_tok = completion_tok = total_tok = 0
        try:
            usage = getattr(completion_response, "usage", None)
            if usage is None and isinstance(completion_response, dict):
                usage = completion_response.get("usage")
            if usage is not None:
                prompt_tok = int(_usage_value(usage, "prompt_tokens") or 0)
                completion_tok = int(_usage_value(usage, "completion_tokens") or 0)
                total_tok = int(_usage_value(usage, "total_tokens") or (prompt_tok + completion_tok))
        except Exception:
            pass

        if not priced_this_call and total_tok and (self.price_in is not None or self.price_out is not None):
            self.cost_usd += (
                prompt_tok * (self.price_in or 0.0) / 1e6
                + completion_tok * (self.price_out or 0.0) / 1e6
            )
            priced_this_call = True

        if priced_this_call:
            self._priced_calls += 1
        self.prompt_tokens += prompt_tok
        self.completion_tokens += completion_tok
        self.total_tokens += total_tok

        model_name = None
        try:
            model_name = getattr(completion_response, "model", None)
            if model_name is None and isinstance(completion_response, dict):
                model_name = completion_response.get("model")
        except Exception:
            pass
        if isinstance(model_name, str) and model_name:
            self._models_seen.setdefault(model_name, None)

    @property
    def priced(self) -> bool:
        return self._priced_calls > 0

    @property
    def models_seen(self) -> list[str]:
        return list(self._models_seen)

    def final_cost_usd(self) -> float | None:
        """Return the accumulated cost, or ``None`` if no call could be priced."""
        if self._priced_calls == 0:
            return None
        return self.cost_usd


SUBAGENTS: list[SubAgent] = [
    {
        "name": "implementer",
        "description": "Writes and edits source code to satisfy the spec. Use for implementation work.",
        "system_prompt": (
            "You implement the requested change with minimal, focused edits. "
            "Follow the repository's conventions (see CLAUDE.md if present). "
            "Never touch files outside the working directory. Do not run git push, merge, or destructive commands."
        ),
    },
    {
        "name": "tester",
        "description": "Writes and runs tests, and reports failures. Use to validate the implementation.",
        "system_prompt": (
            "You write and run tests only. Do not modify non-test source files. "
            "Run the provided test command, summarize pass/fail, and return the failing output tail."
        ),
    },
    {
        "name": "reviewer",
        "description": "Read-only reviewer that returns a prioritized list of issues.",
        "system_prompt": (
            "You review changes for correctness, security, and adherence to the spec. "
            "Return a prioritized, actionable list. You never edit files."
        ),
        "tools": [],
    },
]


def _subagents_with_model(subagents: list[SubAgent], model: ChatLiteLLM) -> list[SubAgent]:
    """Task mode only: pin every subagent to the same model instance as the
    main agent and the rubric grader, so all three share one provider config."""
    out = []
    for sa in subagents:
        sa = dict(sa)
        sa["model"] = model
        out.append(sa)
    return out


MICRO_SYSTEM_PROMPT = (
    "You are a coding worker executing one small, fully specified task from a supervisor. Read the "
    "\"Read first\" files, then make the minimal change described, touching only the allowed files. "
    "Use relative paths. Run the acceptance command; if it fails, fix your change and rerun; when "
    "it exits 0, write a summary of at most 3 sentences and STOP. Do not refactor, rename, "
    "reformat, add features, or edit files outside the allowed list. Never use git for anything "
    "except status/diff/log/add — inspection and staging only, never commit (the supervisor "
    "commits after review); never push, fetch, merge, rebase, stash, or change branches. If the "
    "spec is ambiguous or you have failed the same way three times, call "
    "ask_supervisor or report_blocker instead of guessing. If a tool output ends with a line starting "
    "\"⚠ SUPERVISOR STEERING\", obey it immediately."
)

SYSTEM_PROMPT = (
    "You are an autonomous coding worker delegated by a supervisor. "
    "Work only inside the current working directory; never scan the filesystem or drive root "
    "(no `find /`, no drive-wide searches) — everything you need is in the working directory. "
    "Always use RELATIVE paths for file operations (src/app.js, not /abs/path or C:/...) — "
    "absolute paths are remapped under the virtual root and your files land in the wrong place. "
    "Never run git push, merge, rebase onto other branches, or destructive commands. "
    "Use the implementer/tester/reviewer subagents when helpful. "
    "Communicate upward while you work: call report_progress at each phase transition, "
    "ask_supervisor when the spec is ambiguous or a decision belongs to the user, and "
    "report_blocker after ~3 failed attempts at the same error instead of thrashing. "
    "The supervisor may redirect you at any moment; if a shell command's output or a "
    "report_progress result ends with a line starting '⚠ SUPERVISOR STEERING', that is a "
    "live instruction from the supervisor — treat it as overriding prior guidance on that "
    "point and act on it immediately, on your very next step. "
    "When the definition of done is met and the test command passes, write your final summary "
    "and STOP — do not keep re-verifying. The supervisor reviews and merges."
)

# Task mode's addition to SYSTEM_PROMPT: the model-facing counterpart of the
# git_command_allowed enforcement above, so a blocked command reads as an
# expected constraint instead of a mysterious tool failure.
_TASK_GIT_ALLOWLIST_SENTENCE = (
    "Git is restricted to inspection and local staging — status, diff, log, show, add, blame, "
    "grep, ls-files, rev-parse, apply, rm, mv, restore, safe branch forms, and merge-base; every "
    "other subcommand (commit, push, fetch, pull, merge, rebase, reset, stash, tag, remote, "
    "checkout, switch, and the rest) is blocked at the tool layer, not just discouraged here — "
    "the supervisor commits and integrates after review."
)
TASK_SYSTEM_PROMPT = SYSTEM_PROMPT + " " + _TASK_GIT_ALLOWLIST_SENTENCE


def _message_content(message) -> object | None:
    """Best-effort extraction of a message's textual content."""
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    return content


def _short_note_from_message(message) -> str | None:
    """Return a short human-readable snippet (≤200 chars) from ``message``.

    Returns ``None`` if the message carries no usable text — caller should
    then omit the ``note`` field from the progress payload.
    """
    content = _message_content(message)
    if isinstance(content, str) and content:
        return content[:200]
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        text = " ".join(parts).strip()
        return text[:200] if text else None
    return None


def _last_message_content(messages: list) -> object | None:
    if not messages:
        return None
    return _message_content(messages[-1])


def _bare_model(model_str: str) -> str:
    """Strip the legacy langchain ``'<provider-prefix>:'`` convention.

    ``"litellm:openai/combo-deepseek-main"`` -> ``"openai/combo-deepseek-main"``.
    This is NOT the litellm provider prefix (``openai/``, ``deepseek/``, ...) —
    that one stays, litellm needs it to pick the adapter. This only strips the
    old colon-separated convention some stored profiles still use.
    """
    return model_str.split(":", 1)[-1] if ":" in model_str else model_str


def build_model(
    model: str,
    api_base: str | None,
    api_key: str | None,
    fallback_models: list[str],
    model_kwargs: dict,
) -> ChatLiteLLM:
    """Always return a ChatLiteLLM instance (never a bare string).

    ``model`` keeps its full litellm form including the provider prefix
    (e.g. ``"openai/combo/deepseek-main"``) — litellm uses that prefix to
    pick the adapter; only the legacy ``xxx:`` convention is stripped by
    ``_bare_model``. The same instance this returns is handed to
    create_deep_agent, RubricMiddleware, and every task-mode subagent, so
    they all share one provider config.
    """
    kwargs = dict(model_kwargs) if model_kwargs else {}
    if fallback_models:
        kwargs["fallbacks"] = list(fallback_models)
    return ChatLiteLLM(
        model=_bare_model(model),
        api_base=api_base,
        api_key=api_key,
        model_kwargs=kwargs,
    )


def _opt(v: str | None) -> str | None:
    """CLI convention: an explicit empty string means "unset"."""
    return v if v else None


def _opt_float(v: str) -> float | None:
    v = _opt(v)
    return float(v) if v is not None else None


def _opt_int(v: str) -> int | None:
    v = _opt(v)
    return int(v) if v is not None else None


def _opt_csv(v: str) -> list[str]:
    v = _opt(v)
    return [item.strip() for item in v.split(",") if item.strip()] if v else []


def _opt_json(v: str) -> dict:
    v = _opt(v)
    return json.loads(v) if v else {}


def run_selftest() -> int:
    """Import everything, construct a model and a backend with dummy values,
    make NO network call, print SELFTEST_OK. Used by the server's doctor to
    warm the uv cache and catch a broken install before a real task runs.
    """
    try:
        import fastapi  # noqa: F401  # pinned only for RubricMiddleware's lazy import; verify it resolves here

        build_model("openai/combo/selftest-dummy", None, "dummy-key", [], {})
        with tempfile.TemporaryDirectory() as tmp:
            SupervisedShellBackend(root_dir=tmp, virtual_mode=True, timeout=5, inherit_env=False, env={})
    except Exception as e:  # noqa: BLE001 - report and exit non-zero, don't crash with a traceback
        print(f"SELFTEST_FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print("SELFTEST_OK")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--worktree", default=None)
    p.add_argument("--brief", default=None)
    p.add_argument("--model", default="openai/combo/deepseek-main")
    p.add_argument("--api-base", type=_opt, default=None)
    p.add_argument("--api-key-env-var", default="MONKEY_9ROUTER_KEY",
                    help="Provider-specific env var litellm expects for --model's provider prefix.")
    p.add_argument("--fallback-models", type=_opt_csv, default=[],
                    help="Comma-separated litellm model strings tried in order via litellm's own "
                         "fallback mechanism if the primary model's call fails.")
    p.add_argument("--model-kwargs-json", type=_opt_json, default={},
                    help="JSON object spread into model_kwargs (e.g. temperature).")
    p.add_argument("--price-in", type=_opt_float, default=None)
    p.add_argument("--price-out", type=_opt_float, default=None)
    p.add_argument("--max-budget-usd", type=_opt_float, default=None,
                    help="Stop and report once accumulated cost_usd crosses this cap.")
    p.add_argument("--max-tokens-total", type=_opt_int, default=None,
                    help="Stop and report once accumulated total_tokens crosses this cap — works "
                         "even when the model has no known price.")
    p.add_argument("--mode", choices=["micro", "task"], default="task")
    p.add_argument("--recursion-limit", type=_opt_int, default=None,
                    help="Defaults to 80 in micro mode, 400 in task mode.")
    p.add_argument("--rubric-max-iterations", type=int, default=6)
    p.add_argument("--command-timeout", type=int, default=DEFAULT_COMMAND_TIMEOUT,
                    help="Per-shell-command timeout in seconds (whole process tree killed on expiry).")
    p.add_argument("--allowed-files", type=_opt_csv, default=[],
                    help="Informational only — scope is enforced server-side; the brief already "
                         "lists these for the model.")
    p.add_argument("--test-command", type=_opt, default=None)
    p.add_argument("--definition-of-done", type=_opt, default=None)
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args()

    if args.selftest:
        return run_selftest()

    if not args.worktree or not args.brief:
        p.error("--worktree and --brief are required unless --selftest is given")

    recursion_limit = args.recursion_limit
    if recursion_limit is None:
        recursion_limit = DEFAULT_RECURSION_LIMIT_MICRO if args.mode == "micro" else DEFAULT_RECURSION_LIMIT_TASK

    # MONKEY_WORKER_API_KEY is optional: a keyless profile runs on whatever
    # default auth the target endpoint expects.
    api_key = os.environ.get("MONKEY_WORKER_API_KEY")
    if api_key:
        os.environ[args.api_key_env_var] = api_key

    # Filter the env before handing it to the shell backend so the agent can't
    # echo host secrets (MONKEY_WORKER_API_KEY, the provider key, GITHUB_TOKEN, ...)
    # back through a shell command. PATH / HOME / SystemRoot / TEMP / GIT_CONFIG_*
    # survive so git/node/npm still work. The Python process's own os.environ
    # keeps the provider key — litellm reads it from there.
    shell_env = build_shell_env(args.api_key_env_var)
    backend = SupervisedShellBackend(
        root_dir=args.worktree,
        virtual_mode=True,
        timeout=args.command_timeout,
        inherit_env=False,
        env=shell_env,
        bash_path=_find_bash() if os.name == "nt" else None,
    )

    # Register a litellm success callback to meter cost + tokens across every
    # model call in the run (main agent, subagents, rubric grader). Registered
    # BEFORE create_deep_agent so the first model call is metered too.
    tracker = CostTracker(args.price_in, args.price_out, args.max_tokens_total)
    litellm.success_callback = [tracker]

    model = build_model(args.model, args.api_base, api_key, args.fallback_models, args.model_kwargs_json)

    # _rubric_status is a PrivateStateAttr, omitted from stream()'s final state
    # by design; on_evaluation is the documented way to observe the grader's
    # verdict without a checkpointer.
    rubric_evaluations: list[dict] = []
    middleware = []
    subagents = None
    if args.mode == "task":
        subagents = _subagents_with_model(SUBAGENTS, model)
        middleware.append(RubricMiddleware(
            model=model,
            max_iterations=args.rubric_max_iterations,
            on_evaluation=rubric_evaluations.append,
        ))
        system_prompt = TASK_SYSTEM_PROMPT
    else:
        system_prompt = MICRO_SYSTEM_PROMPT

    agent = create_deep_agent(
        model=model,
        tools=[report_progress, ask_supervisor, report_blocker],
        backend=backend,
        system_prompt=system_prompt,
        subagents=subagents,
        middleware=middleware,
    )

    with open(args.brief, encoding="utf-8") as f:
        prompt = f.read()

    # Rubric only applies in task mode — micro mode has no RubricMiddleware
    # installed and its status is decided by "did the agent stop without error".
    rubric = None
    if args.mode == "task":
        rubric = args.definition_of_done or (
            f"Running `{args.test_command}` succeeds." if args.test_command else None
        )

    invoke_state = {"messages": [{"role": "user", "content": prompt}]}
    if rubric:
        invoke_state["rubric"] = rubric

    result: dict[str, object] = {
        "status": "failed",
        "summary": None,
        "turns": 0,
        "error": None,
        "rubric_status": None,
        "cost_usd": None,
        "total_tokens": None,
        "priced": False,
        "models_seen": [],
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }
    try:
        # Live progress: stream updates one node at a time, print a flushed
        # PROGRESS line per step, and accumulate the longest messages list we
        # see so we can reconstruct the final state for RESULT_JSON. The
        # "last relevant update" for the message history is whichever agent
        # node emitted the full message list — we keep the longest one seen.
        step_counter = 0
        accumulated_messages: list = []
        budget_exceeded = False
        budget_reason = ""
        for update in agent.stream(
            invoke_state,
            config={"recursion_limit": recursion_limit},
            stream_mode="updates",
        ):
            for node_name, node_state in update.items():
                step_counter += 1
                messages_delta: list | None = None
                if isinstance(node_state, dict):
                    delta = node_state.get("messages")
                    if isinstance(delta, list):
                        messages_delta = delta
                if messages_delta is not None and len(messages_delta) > len(accumulated_messages):
                    accumulated_messages = messages_delta

                note = None
                source_for_note = messages_delta if messages_delta else accumulated_messages
                if source_for_note:
                    note = _short_note_from_message(source_for_note[-1])
                payload: dict[str, object] = {"step": step_counter, "node": node_name}
                if note:
                    payload["note"] = note
                emit_progress(payload)

                # Stop as soon as either cap is crossed, rather than running
                # unbounded — checked after every step so the overrun is at
                # most one model call past the cap. The token cap works even
                # when the model has no known price (cost stays 0/unpriced).
                over_budget = args.max_budget_usd is not None and tracker.cost_usd > args.max_budget_usd
                over_tokens = (
                    args.max_tokens_total is not None and tracker.total_tokens > args.max_tokens_total
                )
                if over_budget or over_tokens:
                    budget_exceeded = True
                    budget_reason = (
                        f"cost ${tracker.cost_usd:.4f} crossed the ${args.max_budget_usd:.2f} USD cap"
                        if over_budget
                        else f"{tracker.total_tokens} tokens crossed the {args.max_tokens_total} token cap"
                    )
                    emit_progress({"kind": "report", "note": f"budget exceeded: {budget_reason}; stopping"})
                    break
            if budget_exceeded:
                break

        messages = accumulated_messages
        result["turns"] = len(messages)
        result["summary"] = _last_message_content(messages)
        result["rubric_status"] = rubric_evaluations[-1]["result"] if rubric_evaluations else None
        if budget_exceeded:
            result["status"] = "failed"
            result["error"] = f"budget exceeded: {budget_reason}; stopped early instead of running unbounded"
        elif args.mode == "micro":
            result["status"] = "succeeded"
        elif rubric:
            result["status"] = "succeeded" if result["rubric_status"] == "satisfied" else "failed"
            if result["status"] == "failed":
                result["error"] = f"rubric not satisfied: {result['rubric_status']}"
        else:
            result["status"] = "succeeded"
    except Exception as e:  # noqa: BLE001 - surface any failure to the supervisor as a structured result
        result["error"] = f"{type(e).__name__}: {e}"

    # Metering is recorded regardless of success/failure so the supervisor
    # can still report partial spend on crashed runs.
    result["cost_usd"] = tracker.final_cost_usd()
    result["total_tokens"] = tracker.total_tokens if tracker.total_tokens > 0 else None
    result["priced"] = tracker.priced
    result["models_seen"] = tracker.models_seen
    result["prompt_tokens"] = tracker.prompt_tokens
    result["completion_tokens"] = tracker.completion_tokens

    print(RESULT_MARKER + json.dumps(result))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
