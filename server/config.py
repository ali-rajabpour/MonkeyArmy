"""Global default configuration: state directory, timeouts, caps (§7.0).

No provider/model/API-key configuration lives here — that is entirely
store.py's job (per-profile, in config.json). This module supplies only the
process-wide baseline (`Defaults`) and the state-directory resolution every
other module builds paths from.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
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
    """Process-wide defaults. `config.json`'s `defaults` section overrides
    any of these (store wins) via `load_defaults()` — everything except
    `home`, which is env-controlled, not user-configurable.
    """

    home: Path
    preflight_timeout_s: int = 60
    verify_timeout_s: int = 300
    wait_timeout_s: int = 120
    wait_hard_cap_s: int = 170
    max_diff_lines: int = 300
    integrate_mode: str = "commit"
    probe_ttl_s: int = 600
    conventions_max_chars: int = 4000
    notes_max_chars: int = 2000
    mode: str = "micro"
    # §5.2 `limits` — per-profile config can override any of these; a
    # profile with no `limits` key falls back to exactly these values.
    max_budget_usd: float = 0.50
    max_tokens_total: int = 400000
    timeout_s: int = 900
    stall_s: int = 180
    command_timeout_s: int = 120
    ask_timeout_s: int = 600
    recursion_limit_micro: int = 80
    recursion_limit_task: int = 400
    rubric_max_iterations_task: int = 4


_PATCHABLE_FIELDS = {f.name for f in fields(Defaults)} - {"home"}


def load_defaults() -> Defaults:
    """`Defaults` with `config.json`'s `defaults` section merged on top.

    Reads `config.json` directly rather than importing store.py: store.py
    depends on this module (for `home_dir`/`Defaults`), so the dependency
    must run one way only, or the two modules could not import each other.
    A missing or corrupt config.json degrades to the bare dataclass, same
    as store.load_store() does for its own callers.
    """
    patch: dict[str, Any] = {}
    try:
        raw = json.loads((home_dir() / "config.json").read_text(encoding="utf-8"))
        patch = raw.get("defaults") or {}
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    overrides = {k: v for k, v in patch.items() if k in _PATCHABLE_FIELDS}
    return Defaults(home=home_dir(), **overrides)
