"""Persistent, per-user configuration store for the facade.

Layout on disk (created on first write):

    ~/.monkey-army/config.json       # profiles + default_profile (no secrets)
    ~/.monkey-army/credentials.json  # env-var-name -> API key (facade-managed)

The store is read PER TASK (at run_dev_task time), never cached at server
launch — that is what makes configuration changes apply without restarting
Claude Code. Environment variables remain a fallback so a pre-facade,
env-only setup keeps working unchanged.

API-key resolution order for a profile's `api_key_env_var`:
  1. ~/.monkey-army/credentials.json entry (facade-managed, most intentional)
  2. the OS environment variable itself
  3. legacy MONKEY_WORKER_API_KEY environment variable

Override the store location with MONKEY_ARMY_HOME (used by tests).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_MODEL_RE = re.compile(r"^[a-z0-9_-]+:.+", re.IGNORECASE)


def home_dir() -> Path:
    override = os.environ.get("MONKEY_ARMY_HOME")
    return Path(override) if override else Path.home() / ".monkey-army"


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
        # A corrupt file must not brick every tool; report as empty and let
        # the next write repair it. provider_status surfaces the anomaly.
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load_store() -> dict[str, Any]:
    store = _read_json(config_path())
    store.setdefault("profiles", {})
    store.setdefault("default_profile", None)
    return store


def save_store(store: dict[str, Any]) -> None:
    _write_json(config_path(), store)


def validate_model_string(model: str) -> str | None:
    """Return an error message if `model` is not a litellm-routable string."""
    if not _MODEL_RE.match(model):
        return (
            f"invalid model string {model!r}: expected '<provider-prefix>:<model>' "
            "(e.g. 'litellm:openai/combo-deepseek-main')"
        )
    return None


def set_profile(
    name: str,
    model: str,
    api_key_env_var: str | None = None,
    api_base: str | None = None,
    fallback_models: list[str] | None = None,
) -> dict[str, Any]:
    err = validate_model_string(model)
    if err:
        raise ValueError(err)
    if fallback_models:
        for fm in fallback_models:
            ferr = validate_model_string(fm)
            if ferr:
                raise ValueError(f"invalid fallback model {fm!r}: {ferr}")
    store = load_store()
    profile: dict[str, Any] = {"model": model}
    if api_key_env_var:
        profile["api_key_env_var"] = api_key_env_var
    if api_base:
        profile["api_base"] = api_base
    if fallback_models:
        # Same "<provider-prefix>:<model>" convention as the primary model,
        # for one consistent format in the config file; the worker strips
        # the prefix before handing these to litellm's own fallback kwarg.
        profile["fallback_models"] = fallback_models
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


def store_credential(env_var_name: str, key: str) -> None:
    creds = _read_json(credentials_path())
    creds[env_var_name] = key
    _write_json(credentials_path(), creds)


def get_credential(env_var_name: str) -> str | None:
    return _read_json(credentials_path()).get(env_var_name)


def resolve_profile(profile_name: str | None, env_defaults: dict[str, Any]) -> dict[str, Any]:
    """Resolve the effective model config for a task.

    Returns {"model", "api_key_env_var", "api_base", "api_key", "source"}.
    Raises KeyError listing available profiles when an unknown name is asked.

    With no store (pre-facade setup) and no profile requested, falls back to
    env_defaults — the server's hardcoded config.py defaults.
    """
    store = load_store()
    profiles = store["profiles"]

    if profile_name:
        if profile_name not in profiles:
            available = ", ".join(sorted(profiles)) or "(none defined)"
            raise KeyError(f"unknown profile {profile_name!r}; available: {available}")
        chosen, source = profiles[profile_name], f"profile:{profile_name}"
    elif store["default_profile"] and store["default_profile"] in profiles:
        chosen, source = profiles[store["default_profile"]], f"profile:{store['default_profile']} (default)"
    else:
        chosen, source = env_defaults, "environment (legacy)"

    env_var = chosen.get("api_key_env_var")
    api_key = None
    if env_var:
        api_key = get_credential(env_var) or os.environ.get(env_var)
    api_key = api_key or os.environ.get("MONKEY_WORKER_API_KEY")

    return {
        "model": chosen["model"],
        "api_key_env_var": env_var,
        "api_base": chosen.get("api_base"),
        "api_key": api_key,
        "fallback_models": chosen.get("fallback_models") or [],
        "source": source,
    }


def auth_state(profile: dict[str, Any]) -> dict[str, Any]:
    """Non-secret auth report for one profile: is a key reachable."""
    env_var = profile.get("api_key_env_var")
    key_available = bool(
        (env_var and (get_credential(env_var) or os.environ.get(env_var)))
        or os.environ.get("MONKEY_WORKER_API_KEY")
    )
    return {"api_key_available": key_available}
