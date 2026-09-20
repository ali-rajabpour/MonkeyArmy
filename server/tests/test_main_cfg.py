"""Review-fix section B: config.json's `defaults` section must take effect on
the next tool call, not just after a server restart. The fix replaced
main.py's module-level `cfg = load_defaults()` (frozen at import) with a
`_cfg()` call at the top of every tool body. main.py itself can't be
imported here (it needs the `mcp` package this stdlib-only suite must run
without), so the "no frozen module-level cfg" half is a source-text
assertion, same technique test_launcher_env.py uses for worker.py."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_defaults

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


if __name__ == "__main__":
    unittest.main()
