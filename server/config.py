"""Environment-only configuration (env-config-spec.md, supersedes plan §7.0):
the single place a user configures monkey-army. No profiles, no
config.json/credentials.json, no elicitation — everything a user sets lives
in an environment variable, and a missing or invalid value is a loud error
naming that variable, never a silent default or a second source.

Two entry points:
  `load_defaults()`  — the optional timeouts/caps (`Defaults`), each with a
                        documented default the matching env var overrides.
  `required_config()` — the three required worker variables (base_url,
                        api_key, model) plus the optional worker settings
                        (fallback models, prices, model kwargs), with every
                        missing/invalid one collected into `errors` instead
                        of raising, so a caller (dispatch_task) can report
                        them all in one message before touching anything.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def home_dir() -> Path:
    """State directory: `MONKEY_ARMY_HOME` override or `~/.monkey-army`.

    Read live on every call (never cached) so a test's `setUp` — or a
    supervisor session that changes it mid-run — takes effect immediately.
    """
    override = os.environ.get("MONKEY_ARMY_HOME")
    return Path(override) if override else Path.home() / ".monkey-army"


@dataclass(frozen=True)
class Defaults:
    """Process-wide caps and timeouts. Every field but `home` has a matching
    `MONKEY_*` env var (see `load_defaults`'s `_INT_VARS`/`_FLOAT_VARS`) that
    overrides its documented default; there is no other source.
    """

    home: Path
    preflight_timeout_s: int = 60
    verify_timeout_s: int = 300
    wait_timeout_s: int = 120
    wait_hard_cap_s: int = 170  # hard cap; not configurable (env-config-spec.md)
    max_diff_lines: int = 300
    integrate_mode: str = "commit"
    probe_ttl_s: int = 600
    conventions_max_chars: int = 4000
    notes_max_chars: int = 2000
    max_budget_usd: float = 0.50
    max_tokens_total: int = 400000
    timeout_s: int = 900
    stall_s: int = 180
    command_timeout_s: int = 120
    ask_timeout_s: int = 600
    recursion_limit_micro: int = 80
    recursion_limit_task: int = 400
    rubric_max_iterations_task: int = 4


# field name -> (env var, documented default). Values are read fresh, parsed,
# and validated on every `load_defaults()` call — never clamped, never
# silently ignored (env-config-spec.md).
_INT_VARS: dict[str, tuple[str, int]] = {
    "preflight_timeout_s": ("MONKEY_PREFLIGHT_TIMEOUT_S", 60),
    "verify_timeout_s": ("MONKEY_VERIFY_TIMEOUT_S", 300),
    "wait_timeout_s": ("MONKEY_WAIT_TIMEOUT_S", 120),
    "max_diff_lines": ("MONKEY_MAX_DIFF_LINES", 300),
    "probe_ttl_s": ("MONKEY_PROBE_TTL_S", 600),
    "conventions_max_chars": ("MONKEY_CONVENTIONS_MAX_CHARS", 4000),
    "notes_max_chars": ("MONKEY_NOTES_MAX_CHARS", 2000),
    "max_tokens_total": ("MONKEY_MAX_TOKENS_TOTAL", 400000),
    "timeout_s": ("MONKEY_TIMEOUT_S", 900),
    "stall_s": ("MONKEY_STALL_S", 180),
    "command_timeout_s": ("MONKEY_COMMAND_TIMEOUT_S", 120),
    "ask_timeout_s": ("MONKEY_ASK_TIMEOUT_S", 600),
    "recursion_limit_micro": ("MONKEY_RECURSION_LIMIT_MICRO", 80),
    "recursion_limit_task": ("MONKEY_RECURSION_LIMIT_TASK", 400),
    "rubric_max_iterations_task": ("MONKEY_RUBRIC_MAX_ITERATIONS_TASK", 4),
}
_FLOAT_VARS: dict[str, tuple[str, float]] = {
    "max_budget_usd": ("MONKEY_MAX_BUDGET_USD", 0.50),
}


def _parse_positive_int(env_name: str, raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{env_name} must be a positive integer, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{env_name} must be a positive integer, got {raw!r}")
    return value


def _parse_positive_float(env_name: str, raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{env_name} must be a positive number, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{env_name} must be a positive number, got {raw!r}")
    return value


def load_defaults() -> Defaults:
    """`Defaults` with every set `MONKEY_*` env var parsed and validated over
    the documented default. Raises `ValueError` naming the offending
    variable on a bad value (non-numeric, non-positive, or — for
    `MONKEY_INTEGRATE_MODE` — outside {commit, stage}); an unset variable
    just keeps its default, never an error.
    """
    overrides: dict[str, Any] = {}
    for field_name, (env_name, _default) in _INT_VARS.items():
        raw = os.environ.get(env_name)
        if raw:
            overrides[field_name] = _parse_positive_int(env_name, raw)
    for field_name, (env_name, _default) in _FLOAT_VARS.items():
        raw = os.environ.get(env_name)
        if raw:
            overrides[field_name] = _parse_positive_float(env_name, raw)

    integrate_mode = os.environ.get("MONKEY_INTEGRATE_MODE")
    if integrate_mode:
        if integrate_mode not in ("commit", "stage"):
            raise ValueError(f"MONKEY_INTEGRATE_MODE must be 'commit' or 'stage', got {integrate_mode!r}")
        overrides["integrate_mode"] = integrate_mode

    return Defaults(home=home_dir(), **overrides)


# ── Required worker config (env-config-spec.md) ─────────────────────────

REQUIRED_VARS = ("MONKEY_9ROUTER_BASE_URL", "MONKEY_9ROUTER_KEY", "MONKEY_WORKER_MODEL")


def _missing(env_name: str) -> str:
    return (
        f"{env_name} is not set — export it in the shell you launch Claude Code "
        "from (see .env.example), then restart Claude Code"
    )


def required_config() -> dict[str, Any]:
    """Resolve the env-only worker configuration.

    Returns {base_url, api_key, model, fallback_models, prices, model_kwargs,
    errors}. `errors` lists every missing/invalid variable as one message
    each (env-config-spec.md: report them all at once); when non-empty the
    other fields may be None or partial — callers must check `errors` first
    and never proceed on a partially-resolved config.
    """
    errors: list[str] = []

    base_url = os.environ.get("MONKEY_9ROUTER_BASE_URL") or None
    if not base_url:
        errors.append(_missing("MONKEY_9ROUTER_BASE_URL"))
    elif not (base_url.startswith("http://") or base_url.startswith("https://")):
        errors.append(f"MONKEY_9ROUTER_BASE_URL must start with http:// or https://, got {base_url!r}")

    api_key = os.environ.get("MONKEY_9ROUTER_KEY") or None
    if not api_key:
        errors.append(_missing("MONKEY_9ROUTER_KEY"))

    model = os.environ.get("MONKEY_WORKER_MODEL") or None
    if not model:
        errors.append(_missing("MONKEY_WORKER_MODEL"))
    elif model.startswith("litellm:"):
        errors.append(
            f"MONKEY_WORKER_MODEL is invalid ({model!r}): drop the legacy 'litellm:' prefix "
            "(e.g. 'openai/combo/deepseek-main')"
        )
    elif "/" not in model:
        errors.append(
            f"MONKEY_WORKER_MODEL is invalid ({model!r}): expected 'provider/model' "
            "(e.g. 'openai/combo/deepseek-main')"
        )

    fallback_models = [m.strip() for m in (os.environ.get("MONKEY_FALLBACK_MODELS") or "").split(",") if m.strip()]

    prices: dict[str, float] = {}
    for key, env_name in (("input", "MONKEY_PRICE_INPUT_PER_MTOK"), ("output", "MONKEY_PRICE_OUTPUT_PER_MTOK")):
        raw = os.environ.get(env_name)
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            errors.append(f"{env_name} must be a number, got {raw!r}")
            continue
        if value < 0:
            errors.append(f"{env_name} must be >= 0, got {value!r}")
            continue
        prices[key] = value

    model_kwargs: dict[str, Any] = {}
    kwargs_raw = (os.environ.get("MONKEY_MODEL_KWARGS_JSON") or "").strip()
    if kwargs_raw:
        try:
            parsed = json.loads(kwargs_raw)
        except json.JSONDecodeError as e:
            errors.append(f"MONKEY_MODEL_KWARGS_JSON is not valid JSON: {e}")
        else:
            if isinstance(parsed, dict):
                model_kwargs = parsed
            else:
                errors.append("MONKEY_MODEL_KWARGS_JSON must be a JSON object")

    return {
        "base_url": base_url, "api_key": api_key, "model": model,
        "fallback_models": fallback_models, "prices": prices, "model_kwargs": model_kwargs,
        "errors": errors,
    }
