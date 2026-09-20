"""Review-fix section B: config.json's `defaults` section must take effect on
the next tool call, not just after a server restart. The fix replaced
main.py's module-level `cfg = load_defaults()` (frozen at import) with a
`_cfg()` call at the top of every tool body. main.py itself can't be
imported here (it needs the `mcp` package this stdlib-only suite must run
without), so the "no frozen module-level cfg" half is a source-text
assertion, same technique test_launcher_env.py uses for worker.py."""

from __future__ import annotations

import ast
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import load_defaults
from check_descriptions import find_tools

MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"


class TestLoadDefaultsLiveReload(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        os.environ["MONKEY_ARMY_HOME"] = self._home.name
        self._config_path = Path(self._home.name) / "config.json"

    def tearDown(self):
        os.environ.pop("MONKEY_ARMY_HOME", None)
        self._home.cleanup()

    def _write_max_diff_lines(self, value: int) -> None:
        self._config_path.write_text(json.dumps({"defaults": {"max_diff_lines": value}}), encoding="utf-8")

    def test_second_call_sees_a_value_changed_between_calls(self):
        self._write_max_diff_lines(111)
        first = load_defaults()
        self.assertEqual(first.max_diff_lines, 111)

        self._write_max_diff_lines(222)
        second = load_defaults()
        self.assertEqual(second.max_diff_lines, 222)


class TestMainPyHasNoFrozenModuleLevelCfg(unittest.TestCase):
    def test_no_module_level_cfg_assignment(self):
        source = MAIN_PY.read_text(encoding="utf-8")
        # A module-level (column-0) `cfg = load_defaults()` would freeze
        # defaults at import time, defeating configure(action='set_defaults')
        # until restart — the exact bug this section fixes.
        self.assertIsNone(
            re.search(r"^cfg\s*=\s*load_defaults\(\)", source, re.MULTILINE),
            "main.py must not assign `cfg` at module level; use _cfg() per call",
        )
        self.assertIn("def _cfg() -> Defaults:", source)

    def test_every_tool_body_reloads_cfg(self):
        source = MAIN_PY.read_text(encoding="utf-8")
        tool_count = len(re.findall(r"^@mcp\.tool\(", source, re.MULTILINE))
        cfg_reload_count = len(re.findall(r"^\s+cfg = _cfg\(\)", source, re.MULTILINE))
        self.assertEqual(cfg_reload_count, tool_count)


class TestReviewFixDDescriptionsAndDocstring(unittest.TestCase):
    """§D.9: task_status's description lists the real status set, and the
    module docstring drops the "its own summary line says 12" aside for a
    plain statement — both source-text checks since main.py needs `mcp`."""

    def test_task_status_lists_every_real_status(self):
        source = MAIN_PY.read_text(encoding="utf-8")
        desc = find_tools(source)["task_status"]
        for status in (
            "running", "needs_input", "verifying", "succeeded", "failed",
            "failed_verification", "failed_scope", "failed_oversized",
            "timeout", "cancelled", "integrated",
        ):
            self.assertIn(status, desc, f"task_status description missing status {status!r}")

    def test_module_docstring_states_13_tools_plainly(self):
        source = MAIN_PY.read_text(encoding="utf-8")
        module_docstring = ast.get_docstring(ast.parse(source))
        self.assertIn("13 tools (§6)", module_docstring)
        self.assertNotIn("its own summary line says", module_docstring)


class TestReviewFixDDispatchAndBatchSourceChecks(unittest.TestCase):
    """§D.1/§D.5: dispatch_task validates mode/repo_path and cleans up on
    failure; batch(status|finish) resolves repo_path from batch_id when
    omitted. Source-text checks, same reason as above."""

    def test_dispatch_task_validates_mode_and_repo_path(self):
        source = MAIN_PY.read_text(encoding="utf-8")
        self.assertIn('mode not in ("micro", "task")', source)
        self.assertIn("repo_worktree_error", source)
        self.assertIn("cleanup_job, job)", source)
        self.assertIn("no test_command", source)

    def test_batch_status_and_finish_resolve_repo_from_batch_id(self):
        source = MAIN_PY.read_text(encoding="utf-8")
        self.assertIn("batches.find_manifest", source)


if __name__ == "__main__":
    unittest.main()
