"""9Router / OpenAI-compatible endpoint access — stdlib `urllib.request` only
(§7.7). Three jobs: list what a profile's endpoint serves (`discover_models`),
confirm a profile actually answers with tool-calling before a task spends any
worker tokens on it (`probe`), and a composite health check (`doctor`).
"""

from __future__ import annotations

import functools
import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

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


def _never_raises(fn):
    """Wrap a network entry point so an unexpected failure is a payload, not a
    crashed tool call.

    Field lesson: a JSONDecodeError inside probe escaped all the way out of
    the MCP tool, so the supervisor saw "Error executing tool configure:
    Extra data: line 1 column 578" with no profile, no status, nothing to act
    on. Every failure here belongs in the result.
    """
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - deliberate boundary
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return wrapper


def _parse_body(raw: str) -> Any:
    """Parse a response body that is *supposed* to be one JSON object.

    Real proxies are messier than the spec: 9Router can answer a
    /chat/completions call with server-sent events (`data: {...}` lines, a
    trailing `data: [DONE]`) or append a second document after the first.
    A plain json.loads then raises `Extra data: line 1 column N`, and before
    this the exception escaped the tool call entirely ("Error executing tool
    configure") instead of being reported as a failed probe.

    Order: whole document, then SSE frames (last real one wins — that is the
    completed message), then the first JSON value with trailing bytes
    ignored. Anything else comes back as a structured error carrying a short
    snippet, so the cause is visible without reading server logs.
    """
    raw = raw.strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        pass

    if "data:" in raw:
        frames = [
            line[len("data:"):].strip()
            for line in raw.splitlines()
            if line.strip().startswith("data:")
        ]
        for frame in reversed(frames):
            if not frame or frame == "[DONE]":
                continue
            try:
                return json.loads(frame)
            except ValueError:
                continue

    try:
        value, end = json.JSONDecoder().raw_decode(raw)
    except ValueError:
        return {
            "error": "response was not JSON",
            "snippet": raw[:300],
        }
    if isinstance(value, dict) and end < len(raw):
        value.setdefault("_trailing_bytes", len(raw) - end)
    return value


def _request(
    url: str, api_key: str | None, *, method: str = "GET",
    body: dict[str, Any] | None = None, timeout_s: float = 10,
) -> tuple[int, Any]:
    """One HTTP round trip with a hard TOTAL deadline of `timeout_s`.

    urllib's `timeout` bounds each socket operation, not the request: a
    degraded server that trickles bytes kept a 30 s-timeout probe alive for
    282 s in a live run, and dispatch_task blocks on the probe, so the
    supervisor's tool call would outlive Claude Code's own MCP timeout. The
    round trip runs in a daemon thread and is abandoned at the deadline — the
    thread finishes on its own later; nothing waits for it.
    """
    box: list[tuple[int, Any]] = []
    worker = threading.Thread(
        target=lambda: box.append(_request_once(url, api_key, method, body, timeout_s)),
        daemon=True,
    )
    worker.start()
    worker.join(timeout_s)
    if box:
        return box[0]
    return 0, {"error": f"no complete response within {timeout_s:g}s (endpoint too slow or stalled)"}


def _request_once(
    url: str, api_key: str | None, method: str, body: dict[str, Any] | None, timeout_s: float,
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
            return resp.status, _parse_body(raw)
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = {"error": str(e)}
        return e.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


@_never_raises
def discover_models(profile: dict[str, Any], timeout_s: float = 10) -> dict[str, Any]:
    api_base = profile.get("api_base")
    if not api_base:
        return {"error": "profile has no api_base"}
    status, body = _request(f"{api_base.rstrip('/')}/models", profile.get("api_key"), timeout_s=timeout_s)
    if status != 200:
        return {"error": f"GET /models -> HTTP {status}", "detail": body}
    ids = [m.get("id") for m in (body.get("data") or []) if isinstance(m, dict) and m.get("id")]
    # 9Router's own combos may or may not carry a `combo/` prefix — one
    # deployment advertises `combo/coder`, another plain `coder` (verified
    # against a live router). `combos` is therefore a hint, never the list to
    # choose from: callers offer `models` when it comes back empty.
    combos = [i for i in ids if i.startswith("combo/")]
    return {
        "models": ids,
        "combos": combos,
        "note": (
            "this endpoint does not prefix combos with 'combo/' — pick from `models` and use "
            "the id verbatim after the provider prefix, e.g. openai/<id>"
            if ids and not combos else None
        ),
    }


@_never_raises
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
        # 64, not 16: a tool call costs more tokens than a 16-token budget
        # allows, and a truncated one reads exactly like a model that cannot
        # call tools at all (verified against a live router).
        "model": _bare_model_for_http(profile["model"]), "max_tokens": 64, "temperature": 0,
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
    first = choices[0] if choices else {}
    # Either signal counts: some routers return the parsed `tool_calls` array,
    # others only set finish_reason="tool_calls" after reshaping the payload.
    tool_calls = bool((first.get("message") or {}).get("tool_calls"))
    finished_on_tool_call = first.get("finish_reason") == "tool_calls"
    result = {
        "ok": bool(choices), "latency_ms": latency_ms,
        "model_reported": resp.get("model"),
        "tool_calling": "confirmed" if (tool_calls or finished_on_tool_call) else "not_observed",
        "usage_present": "usage" in resp,
    }
    if not choices:
        result["error"] = "no choices in response"
    _PROBE_CACHE[cache_key] = (now, result)
    return result


def invalidate_probe(profile_name: str | None = None) -> None:
    """Drop a cached probe verdict so the next dispatch_task re-probes
    instead of trusting a result from before a profile edit (model/api_base/
    key change, removal, or a new default). None clears every entry."""
    if profile_name is None:
        _PROBE_CACHE.clear()
    else:
        _PROBE_CACHE.pop(profile_name, None)


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


@_never_raises
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

    cfg_store = store.load_store()
    add("config_present", store.config_path().exists(), str(store.config_path()),
        "configure(action='set_profile', ...) to create one")

    default_profile = cfg_store.get("default_profile")
    add("default_profile", bool(default_profile), default_profile or "none set",
        "configure(action='set_profile', ...) — the first profile becomes default")

    profile = None
    if default_profile:
        try:
            profile = store.resolve_profile(default_profile)
        except KeyError:
            pass
    key_available = bool(profile and profile.get("api_key"))
    add("key_available", key_available,
        "reachable" if key_available else "no key resolvable for the default profile",
        "configure(action='store_key', profile=<name>)")

    if profile:
        probe_result = probe(profile)
        add("probe", bool(probe_result.get("ok")), json.dumps(probe_result), "check api_base/model/key")
    else:
        add("probe", False, "no resolvable default profile", "configure a profile first")

    if profile and profile.get("api_base"):
        models = discover_models(profile)
        ok = "models" in models
        add("models_endpoint", ok,
            ", ".join(models.get("combos", [])) if ok else str(models.get("error")),
            "check 9Router is reachable at api_base")
    else:
        add("models_endpoint", False, "no api_base on default profile", "set api_base on the profile")

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
