"""§7.4 git_ops.integrate tests — real temp git repos, no mocking of git."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import git_ops
from jobs import commit_worktree, create_worktree, stage_files


def _git(cwd: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                           check=check, stdin=subprocess.DEVNULL)


class _GitOpsCase(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        os.environ["MONKEY_ARMY_HOME"] = self._home.name
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = str(Path(self.tmp.name) / "repo")
        Path(self.repo).mkdir()
        _git(self.repo, "init", "-b", "main")
        # The user's own identity — integrate() must commit under THIS, not
        # the worktree's fixed "monkey-army" identity.
        _git(self.repo, "config", "user.name", "Ali Real")
        _git(self.repo, "config", "user.email", "ali@example.com")
        _git(self.repo, "commit", "--allow-empty", "-m", "init")

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("MONKEY_ARMY_HOME", None)
        self._home.cleanup()

    def _succeeded_job(self, filename: str = "new.txt", content: str = "hello\n") -> dict:
        """Simulates what finalize_success leaves behind: a worker commit on
        the monkey branch, ready to review/integrate."""
        wt = create_worktree(self.repo, "main")
        (Path(wt["worktree"]) / filename).write_text(content, encoding="utf-8")
        stage_files(wt["worktree"], [filename])
        commit_worktree(wt["worktree"], "worker commit")
        return {
            **wt, "status": "succeeded", "review": {"verdict": "approve", "feedback": None},
            "filesChanged": [filename], "title": "add a file", "attempt": 1, "model": "test-model",
        }


class TestPreconditions(_GitOpsCase):
    def test_not_approved(self):
        job = self._succeeded_job()
        job["review"] = None
        result = git_ops.integrate(job, self.repo, None, "commit", False)
        self.assertFalse(result["integrated"])
        self.assertEqual(result["reason"], "not_approved")
        # Nothing touched: worktree/branch still there.
        self.assertIn(job["branch"], _git(self.repo, "branch", "--list", "monkey/*").stdout)

    def test_branch_mismatch_blocked_without_allow_flag(self):
        job = self._succeeded_job()
        _git(self.repo, "checkout", "-b", "other")
        result = git_ops.integrate(job, self.repo, None, "commit", False)
        self.assertFalse(result["integrated"])
        self.assertEqual(result["reason"], "branch_mismatch")

    def test_branch_mismatch_bypassed_with_allow_flag(self):
        job = self._succeeded_job()
        _git(self.repo, "checkout", "-b", "other")
        result = git_ops.integrate(job, self.repo, None, "commit", True)
        self.assertTrue(result["integrated"])

    def test_detached_head_always_mismatches_even_with_allow_flag(self):
        job = self._succeeded_job()
        sha = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        _git(self.repo, "checkout", sha)
        result = git_ops.integrate(job, self.repo, None, "commit", True)
        self.assertFalse(result["integrated"])
        self.assertEqual(result["reason"], "branch_mismatch")

    def test_dirty_overlap(self):
        job = self._succeeded_job()
        (Path(self.repo) / "new.txt").write_text("uncommitted local edit\n", encoding="utf-8")
        result = git_ops.integrate(job, self.repo, None, "commit", False)
        self.assertFalse(result["integrated"])
        self.assertEqual(result["reason"], "dirty_overlap")
        self.assertIn("new.txt", result["details"]["files"])

    def test_in_progress_operation(self):
        job = self._succeeded_job()
        (Path(self.repo) / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
        result = git_ops.integrate(job, self.repo, None, "commit", False)
        self.assertFalse(result["integrated"])
        self.assertEqual(result["reason"], "in_progress_operation")


class TestCleanApply(_GitOpsCase):
    def test_commit_mode_uses_user_identity_and_message(self):
        job = self._succeeded_job()
        result = git_ops.integrate(job, self.repo, None, "commit", False)

        self.assertTrue(result["integrated"])
        self.assertEqual(result["mode"], "commit")
        self.assertIn("commit_sha", result)
        self.assertEqual(job["status"], "integrated")
        self.assertTrue((Path(self.repo) / "new.txt").exists())

        author = _git(self.repo, "log", "-1", "--format=%an <%ae>").stdout.strip()
        self.assertEqual(author, "Ali Real <ali@example.com>")
        subject = _git(self.repo, "log", "-1", "--format=%s").stdout.strip()
        self.assertEqual(subject, "add a file")
        body = _git(self.repo, "log", "-1", "--format=%b").stdout
        self.assertIn(job["taskId"], body)

        # Cleanup happened: no monkey/* branch or worktree left.
        self.assertNotIn(job["branch"], _git(self.repo, "branch", "--list", "monkey/*").stdout)
        wt_list = _git(self.repo, "worktree", "list", "--porcelain").stdout
        self.assertNotIn(job["worktree"], wt_list)

    def test_custom_message_is_used_verbatim(self):
        job = self._succeeded_job()
        git_ops.integrate(job, self.repo, "custom subject line", "commit", False)
        subject = _git(self.repo, "log", "-1", "--format=%s").stdout.strip()
        self.assertEqual(subject, "custom subject line")

    def test_stage_mode_stages_without_committing(self):
        job = self._succeeded_job()
        before_sha = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        result = git_ops.integrate(job, self.repo, None, "stage", False)

        self.assertTrue(result["integrated"])
        self.assertEqual(result["mode"], "stage")
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD").stdout.strip(), before_sha, "stage mode must not commit")
        staged = _git(self.repo, "diff", "--cached", "--name-only").stdout
        self.assertIn("new.txt", staged)
        # Still cleans up the worktree/branch.
        self.assertNotIn(job["branch"], _git(self.repo, "branch", "--list", "monkey/*").stdout)

    def test_unrelated_staged_file_survives_untouched(self):
        """§ bugfix: `git commit -m` with no pathspec commits the WHOLE
        index — a user's own unrelated staged work must NOT get swept into
        the integration commit."""
        job = self._succeeded_job()
        (Path(self.repo) / "unrelated.txt").write_text("the user's own work\n", encoding="utf-8")
        _git(self.repo, "add", "unrelated.txt")

        result = git_ops.integrate(job, self.repo, None, "commit", False)

        self.assertTrue(result["integrated"])
        self.assertNotIn("unrelated.txt", result["files"])
        # Still staged — untouched by the integration commit.
        staged = _git(self.repo, "diff", "--cached", "--name-only").stdout.split()
        self.assertEqual(staged, ["unrelated.txt"])
        # And NOT part of the commit that was just made.
        committed_files = _git(self.repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
        self.assertNotIn("unrelated.txt", committed_files)
        self.assertIn("new.txt", committed_files)

    def test_two_disjoint_tasks_integrate_sequentially(self):
        job_a = self._succeeded_job(filename="a.txt", content="a\n")
        job_b = self._succeeded_job(filename="b.txt", content="b\n")

        result_a = git_ops.integrate(job_a, self.repo, None, "commit", False)
        result_b = git_ops.integrate(job_b, self.repo, None, "commit", False)

        self.assertTrue(result_a["integrated"])
        self.assertTrue(result_b["integrated"])
        self.assertNotEqual(result_a["commit_sha"], result_b["commit_sha"])
        self.assertTrue((Path(self.repo) / "a.txt").exists())
        self.assertTrue((Path(self.repo) / "b.txt").exists())

        end = git_ops.assert_end_state(self.repo, [job_a["taskId"], job_b["taskId"]])
        self.assertEqual(end["worktreesLeft"], 0)
        self.assertEqual(end["branchesLeft"], 0)


class TestConflict(_GitOpsCase):
    def test_conflicting_change_leaves_tree_unchanged_and_branch_kept(self):
        job = self._succeeded_job()
        # A conflicting edit landed on main AFTER the worktree branched off it.
        (Path(self.repo) / "new.txt").write_text("a totally different first line\n", encoding="utf-8")
        _git(self.repo, "add", "new.txt")
        _git(self.repo, "commit", "-m", "conflicting change on main")
        before_sha = _git(self.repo, "rev-parse", "HEAD").stdout.strip()

        result = git_ops.integrate(job, self.repo, None, "commit", False)

        self.assertFalse(result["integrated"])
        self.assertEqual(result["reason"], "conflict")
        self.assertIn("new.txt", result["details"]["files"])
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD").stdout.strip(), before_sha, "HEAD must not move")
        self.assertEqual(_git(self.repo, "status", "--porcelain").stdout.strip(), "", "tree must be clean")
        # Branch/worktree preserved so the supervisor can re-dispatch/inspect.
        self.assertIn(job["branch"], _git(self.repo, "branch", "--list", "monkey/*").stdout)


if __name__ == "__main__":
    unittest.main()
