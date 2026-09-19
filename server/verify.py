"""§7.3: preflight validation, post-run verification, scope enforcement, diff
cap, and the finalisation pipeline that turns a worker's claim into the
server's own verdict (I4 — "succeeded" is decided by the server re-running
the acceptance command(s) and checking scope/diff size, never by the
worker's claim).

Preflight lesson from the field: a `test_command` whose *runner* is broken
(not merely failing assertions) makes the worker's acceptance gate
unpassable — it then chases phantom failures forever. Running the command
once in the fresh worktree and surfacing the outcome to the supervisor
catches that before a single worker token is spent.

Mirrors the worker's bash routing on Windows so the preflight sees the same
shell the worker will use.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any

from config import Defaults
from events import publish
from jobs import changed_files, commit_worktree, diff_and_stat, persist_job, stage_files, write_patch
from proc_utils import kill_tree
from statusline_render import write_statusline

OUTPUT_TAIL_CHARS = 800


def find_bash() -> str | None:
    """Locate bash on Windows (same strategy as worker/worker.py)."""
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


def run_command(command: str, cwd: str, timeout_s: int = 120) -> dict[str, Any]:
    """Run ``command`` once in ``cwd``; report exit code + output tail.

    Never raises: any launch failure is folded into the report so the
    caller (preflight, post_run_verify) can pass it along verbatim. Used
    both for the pre-dispatch preflight and every post_run_verify step.
    """
    bash = find_bash() if os.name == "nt" else None
    script_path: str | None = None
    try:
        if bash:
            fd, script_path = tempfile.mkstemp(suffix=".sh", dir=cwd)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(command + "\n")
            popen_args: Any = [bash, script_path]
            shell = False
        else:
            popen_args = command
            shell = True
        proc = subprocess.Popen(
            popen_args, shell=shell, cwd=cwd,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
            start_new_session=(os.name != "nt"),
        )
        try:
            out, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid)
            try:
                out, _ = proc.communicate(timeout=10)
            except Exception:  # noqa: BLE001
                out = ""
            return {
                "ran": True, "exit_code": 124, "timed_out": True,
                "output_tail": (out or "")[-OUTPUT_TAIL_CHARS:],
            }
        return {
            "ran": True, "exit_code": proc.returncode, "timed_out": False,
            "output_tail": (out or "")[-OUTPUT_TAIL_CHARS:],
        }
    except Exception as e:  # noqa: BLE001
        return {"ran": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        if script_path:
            try:
                os.remove(script_path)
            except OSError:
                pass


def preflight_note(report: dict[str, Any]) -> str | None:
    """Supervisor-facing advice derived from a preflight report."""
    if not report.get("ran"):
        return "preflight could not run the test command; check it manually before trusting the worker's gate"
    if report.get("timed_out"):
        return (
            "test command did not finish within the preflight timeout — if it is expected to be slow, "
            "ignore this; otherwise fix it before delegating (a hanging gate stalls the worker)"
        )
    if report.get("exit_code") != 0:
        return (
            "test command exited non-zero. That is NORMAL if tests target code that does not exist yet — "
            "but read output_tail: if the RUNNER itself is broken (module/file not found on the test path, "
            "unknown option), fix test_command and re-delegate, because a broken acceptance gate makes the "
            "worker chase phantom failures"
        )
    return None


# ── Scope enforcement (§7.3) ─────────────────────────────────────────────

def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a `**`-aware glob to a regex, stdlib-only.

    `glob.translate` (3.13+) isn't available on our 3.11 floor, so this is a
    small manual translator: `**` matches any number of path segments
    (including zero — `a/**/b` matches `a/b`), `*` matches within one
    segment, `?` matches one non-`/` character, everything else is literal.
    """
    i, n = 0, len(pattern)
    out: list[str] = []
    while i < n:
        c = pattern[i]
        if c == "*" and pattern[i:i + 2] == "**":
            j = i + 2
            if j < n and pattern[j] == "/":
                out.append("(?:.*/)?")
                j += 1
            else:
                out.append(".*")
            i = j
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def scope_check(changed: list[str], allowed: list[str] | None) -> dict[str, Any]:
    """`{ok, violations, unrestricted}` — empty/None `allowed` means every
    change is in scope (the caller/brief already warned the supervisor)."""
    if not allowed:
        return {"ok": True, "violations": [], "unrestricted": True}
    patterns = [_glob_to_regex(g) for g in allowed]
    violations = [f for f in changed if not any(p.match(f) for p in patterns)]
    return {"ok": not violations, "violations": violations, "unrestricted": False}


def oversize_check(diffstat: dict[str, Any], max_lines: int) -> bool:
    """True when the diff is OVER the cap (i.e. the task should fail)."""
    return diffstat.get("lines", 0) > max_lines


def post_run_verify(job: dict[str, Any], cfg: Defaults) -> dict[str, Any]:
    """Re-run testCommand, then verifyCommand, then lintCommand (whichever
    are set), stopping at the first failure — the server's own acceptance
    gate, independent of anything the worker claimed."""
    steps: list[dict[str, Any]] = []
    passed = True
    for name, key in (("test", "testCommand"), ("verify", "verifyCommand"), ("lint", "lintCommand")):
        command = job.get(key)
        if not command:
            continue
        report = run_command(command, job["worktree"], cfg.verify_timeout_s)
        step = {
            "name": name, "command": command,
            "exitCode": report.get("exit_code"), "timedOut": report.get("timed_out", False),
            "outputTail": report.get("output_tail", ""),
        }
        if not report.get("ran"):
            step["error"] = report.get("error")
        steps.append(step)
        ok = report.get("ran") and not report.get("timed_out") and report.get("exit_code") == 0
        if not ok:
            passed = False
            break
    return {"ranAt": time.time(), "steps": steps, "passed": passed}


# ── Out-of-scope patch section (§7.3) ────────────────────────────────────

def _git_status_porcelain(worktree: str, paths: list[str]) -> str:
    try:
        return subprocess.run(
            ["git", "status", "--porcelain", "--", *paths], cwd=worktree,
            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL, timeout=30,
        ).stdout
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return ""


def _git_diff_unstaged(worktree: str, paths: list[str]) -> str:
    if not paths:
        return ""
    try:
        return subprocess.run(
            ["git", "diff", "--binary", "--", *paths], cwd=worktree,
            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL, timeout=30,
        ).stdout
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return ""


def _out_of_scope_section(worktree: str, violations: list[str]) -> str:
    """Out-of-scope files are deliberately left unstaged (never silently
    dropped): appended to the patch text so a reviewer still sees what the
    worker touched outside its allowlist. Untracked violations have nothing
    committed to diff against, so they're listed by name instead."""
    if not violations:
        return ""
    untracked = {
        line[3:] for line in _git_status_porcelain(worktree, violations).splitlines()
        if line.startswith("??")
    }
    tracked = [v for v in violations if v not in untracked]
    section = ["\n# OUT-OF-SCOPE (unstaged)\n"]
    diff = _git_diff_unstaged(worktree, tracked)
    if diff:
        section.append(diff)
    for path in sorted(untracked):
        section.append(f"# untracked out-of-scope file: {path}\n")
    return "".join(section)


# ── Finalisation pipeline (§7.3) ─────────────────────────────────────────

def finalize_success(job: dict[str, Any], cfg: Defaults) -> None:
    """Server-side verdict: re-verify, enforce scope, cap the diff, commit
    on success. Called by the launcher after a worker's RESULT_JSON claims
    success, or claims failure while still leaving changes on disk (I4 —
    the worker's own verdict never gets the final say).

    Blocking: real git/subprocess work. Callers on an asyncio event loop
    must run this in an executor.
    """

    def _checkpoint(kind: str) -> None:
        persist_job(job)
        write_statusline(job)
        event: dict[str, Any] = {"kind": kind}
        if job.get("error"):
            event["error"] = job["error"]
        publish(job["repo"], job["taskId"], event)

    worktree = job["worktree"]
    job["status"] = "verifying"
    _checkpoint("verifying")

    changed = changed_files(worktree)
    scope = scope_check(changed, job.get("allowedFiles"))
    job["scope"] = scope
    in_scope = [f for f in changed if f not in scope["violations"]]
    stage_files(worktree, in_scope)  # violations stay unstaged, still visible in the patch below

    d = diff_and_stat(worktree, job["baseSha"])  # staged + previously committed attempts
    job["diffstat"] = {"files": d["files"], "added": d["added"], "removed": d["removed"], "lines": d["lines"]}
    job["filesChanged"] = [f["path"] for f in d["files"]]
    patch_text = d["patch"] + _out_of_scope_section(worktree, scope["violations"])
    job["patchPath"] = str(write_patch(job["slug"], job["taskId"], patch_text))

    if not scope["ok"]:
        job["status"] = "failed_scope"
        job["error"] = "out-of-scope files changed: " + ", ".join(scope["violations"])
        _checkpoint("failed_scope")
        return

    if oversize_check(job["diffstat"], cfg.max_diff_lines):
        job["status"] = "failed_oversized"
        job["error"] = f"{job['diffstat']['lines']} changed lines > cap {cfg.max_diff_lines}"
        _checkpoint("failed_oversized")
        return

    verification = post_run_verify(job, cfg)
    job["verification"] = verification
    if not verification["passed"]:
        failing = verification["steps"][-1] if verification["steps"] else None
        job["status"] = "failed_verification"
        job["error"] = (
            f"{failing['name']} exit {failing['exitCode']}" if failing else "verification failed"
        )
        _checkpoint("failed_verification")
        return

    message = f"monkey({job['taskId']}) attempt {job.get('attempt', 1)}: {job.get('title', job['taskId'])}"
    job["commitSha"] = commit_worktree(worktree, message)
    job["status"] = "succeeded"
    _checkpoint("succeeded")
