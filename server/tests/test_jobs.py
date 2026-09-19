"""Job/worktree lifecycle tests against real temporary git repos — stdlib
only. Folds in the former test_jobs_salvage.py."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import (
    changed_files,
    cleanup_job,
    commit_worktree,
    create_worktree,
    diff_and_stat,
    salvage_worktree,
    stage_files,
)
from persistence import all_repos, repo_state_dir, slug_for


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
        stdin=subprocess.DEVNULL,
    )


class JobsTestCase(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        os.environ["MONKEY_ARMY_HOME"] = self._home.name
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = str(Path(self.tmp.name) / "repo")
        Path(self.repo).mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit",
             "--allow-empty", "-m", "init")

    def tearDown(self):
        try:
            _git(self.repo, "worktree", "prune")
        except subprocess.CalledProcessError:
            pass
        self.tmp.cleanup()
        os.environ.pop("MONKEY_ARMY_HOME", None)
        self._home.cleanup()


class TestCreateWorktree(JobsTestCase):
    def test_worktree_lives_outside_the_repo_and_registers_in_repos_index(self):
        wt = create_worktree(self.repo, "main")
        self.assertNotIn(str(Path(self.repo).resolve()), wt["worktree"])
        self.assertTrue(wt["worktree"].startswith(os.environ["MONKEY_ARMY_HOME"]))
        self.assertEqual(wt["slug"], slug_for(self.repo))
        self.assertIn(str(Path(self.repo).resolve()), all_repos())
        self.assertEqual(wt["baseBranch"], "main")
        self.assertTrue(wt["baseSha"])
        self.assertTrue(wt["branch"].startswith("monkey/mk_"))
        self.assertTrue(Path(wt["worktree"]).is_dir())

    def test_default_base_branch_is_current_head(self):
        _git(self.repo, "checkout", "-b", "feature")
        wt = create_worktree(self.repo)
        self.assertEqual(wt["baseBranch"], "feature")


class TestChangedFiles(JobsTestCase):
    def test_lists_new_and_modified_including_spaces(self):
        wt = create_worktree(self.repo, "main")
        (Path(wt["worktree"]) / "plain.txt").write_text("a", encoding="utf-8")
        (Path(wt["worktree"]) / "has space.txt").write_text("b", encoding="utf-8")
        files = changed_files(wt["worktree"])
        self.assertIn("plain.txt", files)
        self.assertIn("has space.txt", files)

    def test_lists_rename_destination_only(self):
        wt = create_worktree(self.repo, "main")
        worktree = wt["worktree"]
        (Path(worktree) / "old_name.txt").write_text("content that is not tiny\n" * 3, encoding="utf-8")
        _git(worktree, "add", "old_name.txt")
        _git(worktree, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "add")
        _git(worktree, "mv", "old_name.txt", "new_name.txt")
        files = changed_files(worktree)
        self.assertIn("new_name.txt", files)
        self.assertNotIn("old_name.txt", files)

    def test_missing_worktree_returns_empty(self):
        self.assertEqual(changed_files(str(Path(self.tmp.name) / "nope")), [])


class TestStageFiles(JobsTestCase):
    def test_stages_exactly_the_given_paths(self):
        wt = create_worktree(self.repo, "main")
        worktree = wt["worktree"]
        (Path(worktree) / "a.txt").write_text("a", encoding="utf-8")
        (Path(worktree) / "b.txt").write_text("b", encoding="utf-8")
        stage_files(worktree, ["a.txt"])
        staged = _git(worktree, "diff", "--cached", "--name-only").stdout.split()
        self.assertEqual(staged, ["a.txt"])

    def test_stages_deletions(self):
        wt = create_worktree(self.repo, "main")
        worktree = wt["worktree"]
        f = Path(worktree) / "gone.txt"
        f.write_text("x", encoding="utf-8")
        _git(worktree, "add", "gone.txt")
        _git(worktree, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "add")
        f.unlink()
        stage_files(worktree, ["gone.txt"])
        staged = _git(worktree, "diff", "--cached", "--name-status").stdout
        self.assertIn("D\tgone.txt", staged)


class TestDiffAndStat(JobsTestCase):
    def test_numbers_match_a_simple_add(self):
        wt = create_worktree(self.repo, "main")
        worktree = wt["worktree"]
        (Path(worktree) / "new.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
        stage_files(worktree, changed_files(worktree))
        d = diff_and_stat(worktree, wt["baseSha"])
        self.assertEqual(d["added"], 3)
        self.assertEqual(d["removed"], 0)
        self.assertEqual(d["lines"], 3)
        self.assertEqual(d["files"], [{"path": "new.txt", "added": 3, "removed": 0}])
        self.assertIn("new.txt", d["patch"])

    def test_no_changes_reports_zero(self):
        wt = create_worktree(self.repo, "main")
        d = diff_and_stat(wt["worktree"], wt["baseSha"])
        self.assertEqual(d, {"patch": "", "files": [], "added": 0, "removed": 0, "lines": 0})


class TestCommitWorktree(JobsTestCase):
    def test_returns_sha_on_a_real_commit(self):
        wt = create_worktree(self.repo, "main")
        worktree = wt["worktree"]
        (Path(worktree) / "f.txt").write_text("x", encoding="utf-8")
        stage_files(worktree, ["f.txt"])
        sha = commit_worktree(worktree, "test commit")
        self.assertIsNotNone(sha)
        self.assertEqual(len(sha), 40)
        log = _git(worktree, "log", "-1", "--format=%an <%ae>").stdout
        self.assertIn("monkey-army <monkey-army@localhost>", log)

    def test_returns_none_when_nothing_staged(self):
        wt = create_worktree(self.repo, "main")
        self.assertIsNone(commit_worktree(wt["worktree"], "nothing to see"))


class TestSalvageWorktree(JobsTestCase):
    def test_salvages_uncommitted_work(self):
        wt = create_worktree(self.repo, "main")
        job = {**wt, "status": "failed"}
        (Path(wt["worktree"]) / "done.js").write_text("module.exports = 42;\n", encoding="utf-8")

        self.assertTrue(salvage_worktree(job))
        self.assertEqual(job["filesChanged"], ["done.js"])
        patch = Path(job["patchPath"]).read_text(encoding="utf-8")
        self.assertIn("module.exports = 42;", patch)
        log = _git(wt["worktree"], "log", "--oneline", "-1").stdout
        self.assertIn("salvage (failed)", log)

    def test_nothing_to_salvage(self):
        wt = create_worktree(self.repo, "main")
        job = {**wt, "status": "failed"}
        self.assertFalse(salvage_worktree(job))
        self.assertNotIn("filesChanged", job)

    def test_salvage_never_raises_on_missing_worktree(self):
        job = {
            "taskId": "mk_x", "repo": self.repo, "slug": slug_for(self.repo),
            "worktree": str(Path(self.tmp.name) / "nope"), "branch": "monkey/mk_x",
            "status": "failed",
        }
        self.assertFalse(salvage_worktree(job))


class TestCleanupJob(JobsTestCase):
    def test_removes_worktree_branch_file_and_comm_dir(self):
        wt = create_worktree(self.repo, "main")
        job = {**wt, "status": "succeeded", "turns": 1, "costUsd": None, "totalTokens": None}
        from persistence import save_job

        save_job(job)
        comm_dir = repo_state_dir(self.repo) / "comm" / job["taskId"]
        comm_dir.mkdir(parents=True, exist_ok=True)
        (comm_dir / "steer.json").write_text("{}", encoding="utf-8")

        result = cleanup_job(job)
        self.assertTrue(result["worktreeRemoved"])
        self.assertTrue(result["branchDeleted"])
        self.assertTrue(result["persistedRemoved"])
        self.assertFalse(Path(wt["worktree"]).exists())
        self.assertFalse(comm_dir.exists())
        branches = _git(self.repo, "branch", "--list", wt["branch"]).stdout
        self.assertEqual(branches.strip(), "")

    def test_delete_branch_false_keeps_the_branch(self):
        wt = create_worktree(self.repo, "main")
        job = {**wt, "status": "succeeded"}
        result = cleanup_job(job, delete_branch=False)
        self.assertFalse(result["branchDeleted"])
        branches = _git(self.repo, "branch", "--list", wt["branch"]).stdout
        self.assertIn(wt["branch"].rsplit("/", 1)[-1], branches)
        _git(self.repo, "branch", "-D", wt["branch"])


if __name__ == "__main__":
    unittest.main()
