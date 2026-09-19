"""Salvage-snapshot tests against a real temporary git repo — stdlib only."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import create_worktree, salvage_worktree, worktree_changed_files


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
        stdin=subprocess.DEVNULL,
    )


class TestSalvageWorktree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = str(Path(self.tmp.name) / "repo")
        Path(self.repo).mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit",
             "--allow-empty", "-m", "init")

    def tearDown(self):
        # Windows: worktree files can be transiently locked; git prune helps.
        try:
            _git(self.repo, "worktree", "prune")
        except subprocess.CalledProcessError:
            pass
        self.tmp.cleanup()

    def test_salvages_uncommitted_work(self):
        wt = create_worktree(".monkey-army", self.repo, "main")
        job = {**wt, "status": "failed"}
        # Simulate the field incident: worker wrote files but died before committing.
        (Path(wt["worktree"]) / "done.js").write_text("module.exports = 42;\n", encoding="utf-8")

        self.assertTrue(salvage_worktree(".monkey-army", job))
        self.assertEqual(job["filesChanged"], ["done.js"])
        patch = Path(job["patchPath"]).read_text(encoding="utf-8")
        self.assertIn("module.exports = 42;", patch)
        # WIP commit landed on the delegate branch.
        log = _git(wt["worktree"], "log", "--oneline", "-1").stdout
        self.assertIn("salvage snapshot", log)

    def test_nothing_to_salvage(self):
        wt = create_worktree(".monkey-army", self.repo, "main")
        job = {**wt, "status": "failed"}
        self.assertFalse(salvage_worktree(".monkey-army", job))
        self.assertNotIn("filesChanged", job)

    def test_worktree_changed_files_lists_uncommitted(self):
        wt = create_worktree(".monkey-army", self.repo, "main")
        self.assertEqual(worktree_changed_files(wt["worktree"]), [])
        (Path(wt["worktree"]) / "index.html").write_text("<canvas>", encoding="utf-8")
        (Path(wt["worktree"]) / "app.js").write_text("//", encoding="utf-8")
        files = worktree_changed_files(wt["worktree"])
        self.assertIn("index.html", files)
        self.assertIn("app.js", files)

    def test_worktree_changed_files_missing_dir_returns_empty(self):
        self.assertEqual(worktree_changed_files(str(Path(self.tmp.name) / "nope")), [])

    def test_salvage_never_raises_on_missing_worktree(self):
        job = {
            "taskId": "mk_x", "repo": self.repo,
            "worktree": str(Path(self.tmp.name) / "nope"), "branch": "monkey/mk_x",
            "status": "failed",
        }
        self.assertFalse(salvage_worktree(".monkey-army", job))


if __name__ == "__main__":
    unittest.main()
