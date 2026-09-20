"""9Router / OpenAI-compatible endpoint access — stdlib `urllib.request` only
(§7.7). Three jobs: list what a profile's endpoint serves (`discover_models`),
confirm a profile actually answers with tool-calling before a task spends any
worker tokens on it (`probe`), and a composite health check (`doctor`).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import config
import store

WORKER_SCRIPT = str(Path(__file__).resolve().parent.parent / "worker" / "worker.py")

# profile name -> (probed_at epoch seconds, result). Process-lifetime only —
# a server restart naturally clears it, which is fine: the next dispatch_task
# probes again.
_PROBE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _bare_model_for_http(model: str) -> str:
    """Strip the litellm provider prefix (e.g. 'openai/') for raw HTTP calls.

    litellm needs the prefix to pick an adapter, but 9Router (and any other
    OpenAI-compatible endpoint reached directly) expects the bare id it
    advertises: 'openai/combo/deepseek-main' -> 'combo/deepseek-main'.
    """
    return model.split("/", 1)[1] if "/" in model else model


def _request(
    url: str, api_key: str | None, *, method: str = "GET",
    body: dict[str, Any] | None = None, timeout_s: float = 10,
) -> tuple[int, Any]:
    """One HTTP round trip; never raises — a transport failure comes back as
    status 0 with an 'error' key, same shape as a parsed error body."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = {"error": str(e)}
        return e.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def discover_models(profile: dict[str, Any], timeout_s: float = 10) -> dict[str, Any]:
    api_base = profile.get("api_base")
    if not api_base:
        return {"error": "profile has no api_base"}
    status, body = _request(f"{api_base.rstrip('/')}/models", profile.get("api_key"), timeout_s=timeout_s)
    if status != 200:
        return {"error": f"GET /models -> HTTP {status}", "detail": body}
    ids = [m.get("id") for m in (body.get("data") or []) if isinstance(m, dict) and m.get("id")]
    return {"models": ids, "combos": [i for i in ids if i.startswith("combo/")]}


def probe(profile: dict[str, Any], ttl_s: int = 600, timeout_s: float = 30) -> dict[str, Any]:
    """POST a tiny tool-calling round trip; cache the verdict per profile
    name for `ttl_s` so dispatch_task doesn't re-probe on every call."""
    cache_key = profile.get("name") or profile.get("model", "")
    now = time.time()
    cached = _PROBE_CACHE.get(cache_key)
    if cached and now - cached[0] < ttl_s:
        return cached[1]

    api_base = profile.get("api_base")
    if not api_base:
        result = {"ok": False, "error": "profile has no api_base"}
        _PROBE_CACHE[cache_key] = (now, result)
        return result

    body = {
        "model": _bare_model_for_http(profile["model"]), "max_tokens": 16, "temperature": 0,
        "messages": [{"role": "user", "content": "Call the tool `ping` with x=1."}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "ping", "description": "health check",
                "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
            },
        }],
        "tool_choice": "auto",
    }
    start = time.time()
    status, resp = _request(
        f"{api_base.rstrip('/')}/chat/completions", profile.get("api_key"),
        method="POST", body=body, timeout_s=timeout_s,
    )
    latency_ms = round((time.time() - start) * 1000)

    if status != 200:
        result = {"ok": False, "latency_ms": latency_ms, "error": f"HTTP {status}: {resp}"}
        _PROBE_CACHE[cache_key] = (now, result)
        return result

    choices = resp.get("choices") or []
    tool_calls = bool(choices) and bool((choices[0].get("message") or {}).get("tool_calls"))
    result = {
        "ok": bool(choices), "latency_ms": latency_ms,
        "model_reported": resp.get("model"),
        "tool_calling": "confirmed" if tool_calls else "not_observed",
        "usage_present": "usage" in resp,
    }
    if not choices:
        result["error"] = "no choices in response"
    _PROBE_CACHE[cache_key] = (now, result)
    return result


def last_probe(profile_name: str) -> dict[str, Any] | None:
    cached = _PROBE_CACHE.get(profile_name)
    if not cached:
        return None
    ts, result = cached
    return {"probed_at": ts, **result}


def all_last_probes() -> dict[str, Any]:
    return {name: last_probe(name) for name in _PROBE_CACHE}


def _worker_selftest() -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["uv", "run", WORKER_SCRIPT, "--selftest"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120,
        )
        ok = proc.returncode == 0 and "SELFTEST_OK" in proc.stdout
        detail = ((proc.stdout or "") + (proc.stderr or ""))[-400:]
        return {"ok": ok, "detail": detail}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


def _repo_orphans(repo_path: str) -> dict[str, Any]:
    try:
        wt_out = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=repo_path,
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15, check=True,
        ).stdout
        # the repo's own primary worktree is always first — everything after
        # it is a monkey-army worktree still hanging around.
        worktrees = max(wt_out.count("worktree "), 1) - 1
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        worktrees = None
    try:
        br_out = subprocess.run(
            ["git", "branch", "--list", "monkey/*"], cwd=repo_path,
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15, check=True,
        ).stdout
        branches = len([line for line in br_out.splitlines() if line.strip()])
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        branches = None
    return {"worktrees": worktrees, "branches": branches}


def doctor(repo_path: str | None = None) -> list[dict[str, Any]]:
    """Composite health check for `configure(action='doctor')` (§6.13)."""
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, fix: str = "") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "fix": fix})

    uv_path = shutil.which("uv")
    add("uv", bool(uv_path), uv_path or "not found on PATH",
        "install uv: https://docs.astral.sh/uv/getting-started/installation/")

    git_path = shutil.which("git")
    git_ver = ""
    if git_path:
        try:
            git_ver = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    add("git", bool(git_path), git_ver or "not found on PATH", "install git")
    add("python", True, sys.version.split()[0])

    resolved = config.required_config()
    add("env_config", not resolved["errors"],
        "; ".join(resolved["errors"]) if resolved["errors"] else "all required variables set",
        "export the missing/invalid MONKEY_* variables (see .env.example)")

    key_available = bool(resolved.get("api_key"))
    add("key_available", key_available,
        "reachable" if key_available else "MONKEY_9ROUTER_KEY is not set",
        "export MONKEY_9ROUTER_KEY in the shell you launch Claude Code from")

    profile = (
        {"name": "default", "model": resolved["model"], "api_base": resolved["base_url"], "api_key": resolved["api_key"]}
        if not resolved["errors"] else None
    )
    if profile:
        probe_result = probe(profile)
        add("probe", bool(probe_result.get("ok")), json.dumps(probe_result), "check MONKEY_9ROUTER_BASE_URL/MONKEY_WORKER_MODEL/MONKEY_9ROUTER_KEY")
    else:
        add("probe", False, "env config invalid — see env_config above", "fix the missing/invalid variables first")

    if profile:
        models = discover_models(profile)
        ok = "models" in models
        add("models_endpoint", ok,
            ", ".join(models.get("combos", [])) if ok else str(models.get("error")),
            "check 9Router is reachable at MONKEY_9ROUTER_BASE_URL")
    else:
        add("models_endpoint", False, "env config invalid — see env_config above", "fix the missing/invalid variables first")

    selftest = _worker_selftest()
    add("worker_selftest", selftest["ok"], selftest["detail"],
        "ensure uv is installed and worker/worker.py's pinned deps resolve")

    if repo_path:
        orphans = _repo_orphans(repo_path)
        add("orphans", (orphans["worktrees"] or 0) == 0 and (orphans["branches"] or 0) == 0,
            f"worktrees={orphans['worktrees']} branches={orphans['branches']}",
            "configure(action='prune', repo_path=...)")
        notes_len = len(store.read_notes(repo_path))
        add("notes_size", notes_len < 4000, f"{notes_len} chars", "prune notes.md if near the cap")

    return checks
