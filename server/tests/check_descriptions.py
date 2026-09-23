#!/usr/bin/env python3
"""WP6 acceptance script (§6): parses server/main.py with `ast` — no `mcp`
import needed, so this runs without installing the server's one dependency —
and asserts the registered tool surface is exactly the 13 tools §6 actually
defines (its own summary line says "12"; that's a known discrepancy in the
plan, not a bug here), each with a description of at most 60 words.

Deliberately named so `python3 -m unittest discover` (pattern `test_*.py`)
never picks it up: run it standalone via `python3 server/tests/check_descriptions.py`.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"

EXPECTED_TOOLS = {
    "dispatch_task", "wait_for_tasks", "task_status", "task_progress",
    "answer_worker", "steer_task", "cancel_task", "task_result",
    "review_task", "integrate_task", "batch", "cleanup_task", "configure",
}

MAX_WORDS = 60


def _is_mcp_tool_decorator(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "tool"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "mcp"
    )


def _description_from(call: ast.Call) -> str | None:
    for kw in call.keywords:
        if kw.arg == "description" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


annotations: dict[str, dict | None] = {}

HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def _annotation_hints(call: ast.Call) -> dict[str, object] | None:
    """The four hints declared on @mcp.tool(annotations=ToolAnnotations(...)).

    Hosts use them to warn before invoking a destructive tool, and OpenAI's
    directory rejects tools where any of the four is missing or non-boolean.
    """
    for kw in call.keywords:
        if kw.arg == "annotations" and isinstance(kw.value, ast.Call):
            return {
                k.arg: getattr(k.value, "value", None)
                for k in kw.value.keywords if k.arg
            }
    return None


def find_tools(source: str) -> dict[str, str | None]:
    """{tool_name: description}, in source order, for every top-level
    function decorated with `@mcp.tool(...)`."""
    tree = ast.parse(source)
    tools: dict[str, str | None] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if _is_mcp_tool_decorator(dec):
                tools[node.name] = _description_from(dec)
                annotations[node.name] = _annotation_hints(dec)
    return tools


def main() -> int:
    tools = find_tools(MAIN_PY.read_text(encoding="utf-8"))
    problems: list[str] = []

    names = set(tools)
    missing = EXPECTED_TOOLS - names
    extra = names - EXPECTED_TOOLS
    if missing:
        problems.append(f"missing tools: {sorted(missing)}")
    if extra:
        problems.append(f"unexpected tools (§6 lists exactly 13): {sorted(extra)}")

    for name, desc in sorted(tools.items()):
        if not desc:
            problems.append(f"{name}: no description")
            continue
        words = len(desc.split())
        print(f"{name}: {words} words")
        if words > MAX_WORDS:
            problems.append(f"{name}: description is {words} words (limit {MAX_WORDS})")

    for name in sorted(tools):
        hints = annotations.get(name)
        if hints is None:
            problems.append(f"{name}: no annotations=ToolAnnotations(...)")
            continue
        for hint in HINTS:
            if not isinstance(hints.get(hint), bool):
                problems.append(f"{name}: {hint} missing or not a literal bool")

    if problems:
        print("\nFAILED:")
        for p in problems:
            print(f" - {p}")
        return 1

    print(f"\nOK: {len(tools)} tools, all ≤ {MAX_WORDS} words, all four annotation hints declared, matches the exact §6 set.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
