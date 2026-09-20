"""§7.3 finalize_success pipeline tests — real temp git repos (no mocking of
git itself), covering every terminal branch the launcher can land on after a
worker's RESULT_JSON."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Defaults
from jobs import create_worktree
from verify import finalize_success


def _git(cwd: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                    check=True, stdin=subprocess.DEVNULL)


class _FinalizeCase(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        os.environ["MONKEY_ARMY_HOME"] = self._home.name
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = str(Path(self.tmp.name) / "repo")
        Path(self.repo).mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "--allow-empty", "-m", "init")

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("MONKEY_ARMY_HOME", None)
        self._home.cleanup()

    def _job(self, **overrides) -> dict:
        wt = create_worktree(self.repo, "main")
        job = {
            **wt, "status": "running", "attempt": 1, "title": "test task",
            "allowedFiles": [], "testCommand": None, "verifyCommand": None, "lintCommand": None,
        }
        job.update(overrides)
        return job

    def _cfg(self, **overrides) -> Defaults:
        kwargs = {"max_diff_lines": 300, "verify_timeout_s": 10, **overrides}
        return Defaults(home=Path(self._home.name), **kwargs)


class TestSucceeded(_FinalizeCase):
    def test_succeeded_commits_and_reports_diffstat(self):
        job = self._job()
        (Path(job["worktree"]) / "new.txt").write_text("hello\n", encoding="utf-8")
        finalize_success(job, self._cfg())

        self.assertEqual(job["status"], "succeeded")
        self.assertIsNotNone(job["commitSha"])
        self.assertIn("new.txt", job["filesChanged"])
        self.assertEqual(job["diffstat"]["lines"], 1)
        self.assertTrue(Path(job["patchPath"]).exists())
        self.assertIn("new.txt", Path(job["patchPath"]).read_text(encoding="utf-8"))
        # Committed under the fixed monkey-army identity, not the user's.
        author = subprocess.run(["git", "log", "-1", "--format=%an"], cwd=job["worktree"],
                                 capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(author, "monkey-army")

    def test_ignores_the_workers_own_claimed_status(self):
        """finalize_success is the server's own verdict (I4) — it never
        reads workerClaimedStatus, so a worker that claimed 'failed' but
        left real changes still gets a real 'succeeded' from the pipeline."""
        job = self._job(workerClaimedStatus="failed")
        (Path(job["worktree"]) / "new.txt").write_text("hello\n", encoding="utf-8")
        finalize_success(job, self._cfg())
        self.assertEqual(job["status"], "succeeded")


class TestFailedScope(_FinalizeCase):
    def test_out_of_scope_file_fails_scope_and_leaves_no_commit(self):
        job = self._job(allowedFiles=["allowed/**/*.py"])
        (Path(job["worktree"]) / "new.txt").write_text("out of scope\n", encoding="utf-8")
        finalize_success(job, self._cfg())

        self.assertEqual(job["status"], "failed_scope")
        self.assertIn("new.txt", job["scope"]["violations"])
        self.assertIsNone(job.get("commitSha"))
        patch_text = Path(job["patchPath"]).read_text(encoding="utf-8")
        self.assertIn("OUT-OF-SCOPE", patch_text)
        self.assertIn("new.txt", patch_text)

    def test_in_scope_file_alongside_a_violation_stays_staged(self):
        job = self._job(allowedFiles=["allowed.txt"])
        (Path(job["worktree"]) / "allowed.txt").write_text("ok\n", encoding="utf-8")
        (Path(job["worktree"]) / "not_allowed.txt").write_text("nope\n", encoding="utf-8")
        finalize_success(job, self._cfg())

        self.assertEqual(job["status"], "failed_scope")
        self.assertIn("not_allowed.txt", job["scope"]["violations"])
        self.assertNotIn("allowed.txt", job["scope"]["violations"])


class TestFailedOversized(_FinalizeCase):
    def test_diff_over_the_cap_fails_oversized(self):
        job = self._job()
        (Path(job["worktree"]) / "big.txt").write_text("\n".join(f"line {i}" for i in range(50)), encoding="utf-8")
        finalize_success(job, self._cfg(max_diff_lines=5))

        self.assertEqual(job["status"], "failed_oversized")
        self.assertIn("cap", job["error"])
        self.assertIsNone(job.get("commitSha"))


class TestFailedVerification(_FinalizeCase):
    def test_failing_test_command_fails_verification(self):
        job = self._job(testCommand="exit 1")
        (Path(job["worktree"]) / "new.txt").write_text("hello\n", encoding="utf-8")
        finalize_success(job, self._cfg())

        self.assertEqual(job["status"], "failed_verification")
        self.assertFalse(job["verification"]["passed"])
        self.assertEqual(job["verification"]["steps"][0]["name"], "test")
        self.assertIsNone(job.get("commitSha"))

    def test_verify_command_runs_after_a_passing_test_command(self):
        job = self._job(testCommand="exit 0", verifyCommand="exit 1")
        (Path(job["worktree"]) / "new.txt").write_text("hello\n", encoding="utf-8")
        finalize_success(job, self._cfg())

        self.assertEqual(job["status"], "failed_verification")
        self.assertEqual([s["name"] for s in job["verification"]["steps"]], ["test", "verify"])


class TestFailedScopeCommittedFiles(_FinalizeCase):
    """review-fix §C.1: `commit` used to be in the worker's git allowlist,
    so a committed out-of-scope file left nothing in `changed_files`'
    uncommitted porcelain — the scope check saw a clean worktree while
    diff_and_stat(baseSha) still carried the file into the patch. The scope
    check must also see anything committed since baseSha."""

    def test_committed_out_of_scope_file_is_still_caught(self):
        job = self._job(allowedFiles=["allowed/**/*.py"])
        wt = job["worktree"]
        (Path(wt) / "sneaky.txt").write_text("out of scope, committed\n", encoding="utf-8")
        _git(wt, "add", "sneaky.txt")
        _git(wt, "-c", "user.name=w", "-c", "user.email=w@w", "commit", "-m", "sneaky")

        finalize_success(job, self._cfg())

        self.assertEqual(job["status"], "failed_scope")
        self.assertIn("sneaky.txt", job["scope"]["violations"])
        self.assertIn("sneaky.txt", Path(job["patchPath"]).read_text(encoding="utf-8"))


class TestBranchHistoryTamper(_FinalizeCase):
    """review-fix §C.1: HEAD must still descend from baseSha before
    anything else runs — a stray reset moving HEAD backward (or sideways)
    must not reach staging/scope-check/commit at all."""

    def test_head_reset_before_base_fails_closed(self):
        # Give main a second commit so baseSha has a parent to reset back to.
        _git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "--allow-empty", "-m", "second")
        job = self._job()
        wt = job["worktree"]
        _git(wt, "reset", "--hard", f"{job['baseSha']}~1")

        finalize_success(job, self._cfg())

        self.assertEqual(job["status"], "failed_scope")
        self.assertEqual(job["error"], "branch history tampered: HEAD no longer descends from baseSha")
        self.assertIsNone(job.get("commitSha"))
        self.assertIsNone(job.get("patchPath"))


if __name__ == "__main__":
    unittest.main()
