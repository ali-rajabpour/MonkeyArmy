"""Persistent, per-user configuration store: profiles, credentials,
defaults, and the repos index (re-exported from persistence.py — see that
module's docstring for why the repos-index I/O lives there).

Layout under home_dir() (config.home_dir):

    config.json        {"default_profile": ..., "profiles": {...}, "defaults": {...}}
    credentials.json   {"<ENV_VAR_NAME>": "<key>"}   mode 0600
    repos.json         {"<slug>": "/abs/repo/path"}

No secrets ever live in config.json. Everything here is read fresh on every
call (never cached at server launch) so configuration changes apply without
restarting Claude Code.

API-key resolution order for a profile's `api_key_env_var`:
  1. credentials.json entry (facade-managed, most intentional)
  2. the OS environment variable itself
There is no legacy-env fallback model — WP0 removed the old env-only config
path entirely.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Any

from config import load_defaults
from config import home_dir as home_dir  # re-exported: "Keep: ... home_dir()" (§7.1)
from persistence import TERMINAL
from persistence import (
    all_repos as all_repos,
    remember_repo as remember_repo,
    repo_state_dir as repo_state_dir,
    slug_for as slug_for,
)

_LEGACY_PREFIX_RE = re.compile(r"^litellm:", re.IGNORECASE)


def config_path() -> Path:
    return home_dir() / "config.json"


def credentials_path() -> Path:
    return home_dir() / "credentials.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        # A corrupt file must not brick every tool; the next write repairs it.
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    # 0600: credentials.json holds secrets; harmless (and applied) for every
    # other store file written through this helper too.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_store() -> dict[str, Any]:
    store = _read_json(config_path())
    store.setdefault("profiles", {})
    store.setdefault("default_profile", None)
    store.setdefault("defaults", {})
    store.setdefault("meta", {})
    return store


def save_store(store: dict[str, Any]) -> None:
    _write_json(config_path(), store)


# ── Validation (§5.2) ────────────────────────────────────────────────────

def validate_model_string(model: str) -> str | None:
    """Error message if `model` is not a usable litellm model string, else None.

    Decision (plan §5.2 left "or a known bare name" unspecified — no such
    list exists anywhere in the spec): require a `provider/model` shape.
    Simplest option consistent with the goal; a genuinely bare model name
    can be added as a follow-up if 9Router ever needs one.
    """
    if not isinstance(model, str) or not model.strip():
        return "model must be a non-empty string"
    if _LEGACY_PREFIX_RE.match(model):
        return (
            f"invalid model string {model!r}: drop the legacy 'litellm:' prefix "
            "(e.g. 'openai/combo/deepseek-main')"
        )
    if "/" not in model:
        return (
            f"invalid model string {model!r}: expected 'provider/model' "
            "(e.g. 'openai/combo/deepseek-main')"
        )
    return None


def validate_api_base(api_base: str | None) -> str | None:
    if api_base is None:
        return None
    if not (api_base.startswith("http://") or api_base.startswith("https://")):
        return f"invalid api_base {api_base!r}: must start with http:// or https://"
    return None


def validate_prices(price_per_mtok: dict[str, Any] | None) -> str | None:
    if not price_per_mtok:
        return None
    for key in ("input", "output"):
        value = price_per_mtok.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return f"invalid price_per_mtok.{key} = {value!r}: must be a number >= 0"
    return None


def validate_limits(limits: dict[str, Any] | None) -> str | None:
    if not limits:
        return None
    for key, value in limits.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return f"invalid limits.{key} = {value!r}: must be a positive number"
    return None


# ── Profile CRUD ─────────────────────────────────────────────────────────

def set_profile(
    name: str,
    model: str,
    api_key_env_var: str | None = None,
    api_base: str | None = None,
    fallback_models: list[str] | None = None,
    price_per_mtok: dict[str, float] | None = None,
    model_kwargs: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    err = validate_model_string(model)
    if err:
        raise ValueError(err)
    if fallback_models:
        for fm in fallback_models:
            ferr = validate_model_string(fm)
            if ferr:
                raise ValueError(f"invalid fallback model {fm!r}: {ferr}")
    for err in (
        validate_api_base(api_base),
        validate_prices(price_per_mtok),
        validate_limits(limits),
    ):
        if err:
            raise ValueError(err)

    store = load_store()
    profile: dict[str, Any] = {"model": model}
    if api_key_env_var:
        profile["api_key_env_var"] = api_key_env_var
    if api_base:
        profile["api_base"] = api_base
    if fallback_models:
        profile["fallback_models"] = fallback_models
    if price_per_mtok:
        profile["price_per_mtok"] = price_per_mtok
    if model_kwargs:
        profile["model_kwargs"] = model_kwargs
    if limits:
        profile["limits"] = limits
    store["profiles"][name] = profile
    if store["default_profile"] is None:
        store["default_profile"] = name
    save_store(store)
    return profile


def remove_profile(name: str) -> bool:
    store = load_store()
    if name not in store["profiles"]:
        return False
    del store["profiles"][name]
    if store["default_profile"] == name:
        store["default_profile"] = next(iter(store["profiles"]), None)
    save_store(store)
    return True


def set_default_profile(name: str) -> None:
    store = load_store()
    if name not in store["profiles"]:
        raise KeyError(name)
    store["default_profile"] = name
    save_store(store)


def reset_config() -> dict[str, bool]:
    """Delete config.json and credentials.json — every profile, the default,
    the defaults block and every stored key.

    Repo state (jobs, patches, notes, worktrees) is deliberately untouched:
    resetting the provider configuration is not the same as discarding work
    in flight. Returns which files actually existed.
    """
    removed = {}
    for label, path in (("config", config_path()), ("credentials", credentials_path())):
        try:
            path.unlink()
            removed[f"{label}_removed"] = True
        except FileNotFoundError:
            removed[f"{label}_removed"] = False
    return removed


def store_credential(env_var_name: str, key: str) -> None:
    creds = _read_json(credentials_path())
    creds[env_var_name] = key
    _write_json(credentials_path(), creds)


def get_credential(env_var_name: str) -> str | None:
    return _read_json(credentials_path()).get(env_var_name)


# ── Defaults (§7.0) ──────────────────────────────────────────────────────

def get_defaults() -> dict[str, Any]:
    return dict(load_store().get("defaults") or {})


def set_defaults(patch: dict[str, Any]) -> dict[str, Any]:
    store = load_store()
    store["defaults"] = {**(store.get("defaults") or {}), **patch}
    save_store(store)
    return store["defaults"]


# ── Resolution ───────────────────────────────────────────────────────────

_LIMIT_FIELDS = (
    "max_budget_usd", "max_tokens_total", "timeout_s", "stall_s",
    "command_timeout_s", "ask_timeout_s", "recursion_limit_micro",
    "recursion_limit_task", "rubric_max_iterations_task",
)


def resolve_profile(name: str | None = None) -> dict[str, Any]:
    """Resolve the effective config for a task.

    Returns {name, model, api_base, api_key_env_var, api_key,
    fallback_models, price_per_mtok, model_kwargs, limits, source}.
    Raises KeyError (listing available profiles, or saying none exist) when
    the request cannot be satisfied.
    """
    store = load_store()
    profiles = store["profiles"]
    if not profiles:
        raise KeyError(
            "no profiles configured; use configure(action='set_profile', ...) to add one"
        )
    if name:
        if name not in profiles:
            available = ", ".join(sorted(profiles))
            raise KeyError(f"unknown profile {name!r}; available: {available}")
        chosen_name, source = name, f"profile:{name}"
    elif store["default_profile"] in profiles:
        chosen_name = store["default_profile"]
        source = f"profile:{chosen_name} (default)"
    else:
        # Store has profiles but no recorded default — shouldn't normally
        # happen (set_profile always sets one on first use); fall back to
        # the first rather than erroring on a merely inconsistent file.
        chosen_name = next(iter(profiles))
        source = f"profile:{chosen_name} (first, no default set)"
    chosen = profiles[chosen_name]

    env_var = chosen.get("api_key_env_var")
    api_key = (get_credential(env_var) or os.environ.get(env_var)) if env_var else None

    defaults = load_defaults()
    limits: dict[str, Any] = {field: getattr(defaults, field) for field in _LIMIT_FIELDS}
    limits.update(chosen.get("limits") or {})

    return {
        "name": chosen_name,
        "model": chosen["model"],
        "api_base": chosen.get("api_base"),
        "api_key_env_var": env_var,
        "api_key": api_key,
        "fallback_models": chosen.get("fallback_models") or [],
        "price_per_mtok": chosen.get("price_per_mtok"),
        "model_kwargs": chosen.get("model_kwargs") or {},
        "limits": limits,
        "source": source,
    }


def auth_state(profile: dict[str, Any]) -> dict[str, Any]:
    """Non-secret auth report for one profile: is a key reachable."""
    env_var = profile.get("api_key_env_var")
    key_available = bool(env_var and (get_credential(env_var) or os.environ.get(env_var)))
    return {"api_key_available": key_available}


# ── Notes (§7.1, §5.1) ───────────────────────────────────────────────────

_NOTES_HARD_CAP_CHARS = 4000  # file size cap; distinct from Defaults.notes_max_chars,
# which trims how much gets INJECTED into a worker brief.


def notes_path(repo_path: str | Path) -> Path:
    return repo_state_dir(repo_path) / "notes.md"


def read_notes(repo_path: str | Path, max_chars: int | None = None) -> str:
    try:
        text = notes_path(repo_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    return text if max_chars is None else text[-max_chars:]


def append_note(repo_path: str | Path, text: str) -> None:
    """Append a dated line; refuse if the file would grow past 4000 chars."""
    path = notes_path(repo_path)
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    line = f"- {date.today().isoformat()}: {text.strip()}\n"
    combined = existing + line
    if len(combined) > _NOTES_HARD_CAP_CHARS:
        raise ValueError(
            f"notes.md would exceed {_NOTES_HARD_CAP_CHARS} chars ({len(combined)}); "
            "prune old notes first"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(combined, encoding="utf-8")


def record_doctor_run() -> float:
    """Persist meta.last_doctor_at — configure(action='status') surfaces it
    so the supervisor can see how stale the last health check is."""
    st = load_store()
    now = time.time()
    st["meta"]["last_doctor_at"] = now
    save_store(st)
    return now


# ── Maintenance (§6.13 `configure(action='prune')`) ─────────────────────

def prune_repo(repo: str, older_than_days: int) -> dict[str, Any]:
    """Delete logs/patches/jobs of TERMINAL tasks older than N days, and
    `git worktree prune` the repo. Never raises — a corrupt job file is
    skipped, not fatal to the sweep."""
    cutoff = time.time() - older_than_days * 86400
    jobs_dir = repo_state_dir(repo) / "jobs"
    removed_jobs = removed_patches = removed_logs = 0
    for job_file in (jobs_dir.glob("*.json") if jobs_dir.is_dir() else []):
        try:
            job = json.loads(job_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("status") not in TERMINAL:
            continue
        ts = job.get("finishedAt") or job.get("startedAt") or 0
        if ts and ts > cutoff:
            continue
        task_id = job.get("taskId") or job_file.stem
        job_file.unlink(missing_ok=True)
        removed_jobs += 1
        patch = repo_state_dir(repo) / "patches" / f"{task_id}.diff"
        if patch.exists():
            patch.unlink()
            removed_patches += 1
        log = repo_state_dir(repo) / "logs" / f"{task_id}.jsonl"
        if log.exists():
            log.unlink()
            removed_logs += 1
    try:
        subprocess.run(["git", "worktree", "prune"], cwd=repo, capture_output=True, text=True,
                        stdin=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {"repo": repo, "jobs_removed": removed_jobs, "patches_removed": removed_patches, "logs_removed": removed_logs}
