"""Global default configuration (dirs, limits).

Placeholder: hardcoded defaults only, no env-var reads. The full profile/
store-driven config (§7 of the implementation plan) replaces this module.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    worker_api_key: str | None
    api_key_env_var: str
    model: str
    default_recursion_limit: int
    default_rubric_max_iterations: int
    default_max_budget_usd: float
    default_timeout_ms: int
    work_dir: str
    command_timeout_s: int
    stall_timeout_s: int


def load_config() -> Config:
    return Config(
        worker_api_key=None,
        api_key_env_var="MONKEY_9ROUTER_KEY",
        model="openai/combo/deepseek-main",
        default_recursion_limit=400,
        default_rubric_max_iterations=6,
        default_max_budget_usd=5,
        default_timeout_ms=1800000,
        work_dir=".monkey-army",
        command_timeout_s=120,
        stall_timeout_s=300,
    )
