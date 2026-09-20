# Validation plan

All phases use a throwaway copy of `examples/toy-repo` under `/tmp` — never point at a real
repository before Phase 5.

Set up the scratch repo once per validation pass:

```bash
cp -r examples/toy-repo /tmp/toy && cd /tmp/toy && git init -b main && git add . && git commit -m init
```

Run the existing test suite (from the plugin repo root) with:

```bash
python3 -m unittest discover -s server/tests
uv run worker/tests/test_worker.py
```

## Phase 0 — environment (no code)

- [ ] `curl -sS "$BASE/models" -H "Authorization: Bearer $KEY"` lists the combo.
- [ ] One `chat/completions` call with a `tools` array to `combo/<id>` returns `tool_calls` and
      `usage`; note the `model` field in the response.
- [ ] `uv --version` and `git --version` both succeed.
- [ ] `python3 -m unittest discover -s server/tests` is green.
- [ ] `uv run worker/tests/test_worker.py` is green.
- [ ] 9Router dashboard: RTK / Caveman tool-result compression is OFF for the worker key.

## Phase 1 — plumbing (after WP3/WP4)

- [ ] T1.1: `configure(action="doctor")` is all green; `configure(action="discover_models",
      profile=<default>)` lists the combo; `configure(action="probe", profile=<default>)`
      returns `tool_calling: "confirmed"`.
- [ ] T1.2 (TDD split — the toy suite has no `subtract` test yet, so there is nothing to fail
      until task A writes it):
      - Task A: dispatch "add `test_subtract` to `tests/test_calc.py`, covering
        `subtract(a, b)` for positive, negative and zero cases; do not implement `subtract`
        itself" against `/tmp/toy`, `allowed_files=["tests/test_calc.py"]`. Job ends
        `succeeded`. Review the new test — it should fail for the right reason (`subtract` does
        not exist yet) — then approve and integrate.
      - Task B: dispatch "implement `subtract(a, b)` in `calc/__init__.py` so
        `tests/test_calc.py::test_subtract` passes" against `/tmp/toy`,
        `allowed_files=["calc/__init__.py"]`, `test_command="uv run --no-project --with pytest
        python -m pytest -q"`. Job ends `succeeded`, `verification.passed` true, `priced:true`,
        `models_seen` non-empty.
- [ ] T1.3: during the run, `git status` inside `/tmp/toy` stays clean, and the worktree appears
      under `~/.monkey-army/repos/<slug>/worktrees/<task_id>/`, not inside `/tmp/toy`.
- [ ] T1.4 (compression check): dispatch a task that prints `fixtures/sentinel.txt` with `cat`
      and copies it verbatim into `fixtures/copy.txt`; inside the worktree,
      `cmp fixtures/sentinel.txt fixtures/copy.txt` reports no differences.
- [ ] T1.5: dispatch with a temp profile pointing at a wrong model name → `probe` fails and
      `dispatch_task` refuses before any worktree is created.

## Phase 2 — gates and merge-back (after WP5)

- [ ] T2.1: dispatch a task whose worker edits a file outside `allowed_files` → job ends
      `failed_scope`; `/tmp/toy` main tree untouched.
- [ ] T2.2: dispatch a spec that instructs the worker to "skip the tests" against a repo with a
      failing test → job ends `failed_verification` (server re-ran the test itself).
- [ ] T2.3: dispatch two parallel tasks with disjoint `allowed_files`, approve both, run
      `batch(action="finish", ...)` → one worktree left, zero `monkey/*` branches, two commits on
      `main`, `pytest` green.
- [ ] T2.4: dispatch two tasks that both touch the same file; integrate the first, then attempt
      `integrate_task` on the second → `integrated:false, reason:"conflict"`, tree left clean,
      branch preserved; re-dispatch the second task against the updated `main` and confirm it now
      integrates.
- [ ] T2.5: `integrate_task(task_id, mode="stage")` → changes staged (not committed), worktree
      and branch cleaned up.
- [ ] T2.6: restart Claude Code mid-task → `task_status(task_id)` still resolves correctly after
      restart; `cancel_task(task_id)` works on the now-orphaned job; salvaged patch is present.
- [ ] T2.7: dispatch a task engineered to produce > 300 changed lines → job ends
      `failed_oversized`.
- [ ] T2.8: dispatch a task whose worker is expected to ask a clarifying question →
      `wait_for_tasks` returns early with the question; `answer_worker(task_id, answer)` resumes
      it and the stall clock resets.
- [ ] T2.9: `review_task(task_id, "reject", feedback="<specific problem>")` → attempt 2 runs in
      the same worktree; the final diff includes both attempts' changes; `integrate_task`
      produces one squash commit covering both.

## Phase 3 — the loop with Opus, A/B (user-run; prepare, don't fabricate)

Implement the same feature (~5 micro-tasks, ~150 lines) twice from fresh sessions:

- [ ] (A) Opus alone, no delegation — record `/cost`.
- [ ] (B) `/monkey-army` — record `/cost` and the batch report.
- [ ] Confirm identical test results between (A) and (B).
- [ ] Do a quick quality read of both diffs.
- [ ] Fill the results table in `docs/TOKEN-ECONOMICS.md`. Target: B total ≤ 0.6 × A with equal
      quality; if not, widen task granularity and repeat before Phase 5.

## Phase 4 — failure drills

- [ ] Stop 9Router mid-task → job follows the stall/timeout path, salvage patch present, error
      message is clear.
- [ ] Dispatch with a broken `test_command` (e.g. wrong test runner flag) → the preflight note
      surfaces the broken command instead of the job proceeding silently.
- [ ] Dispatch with `max_budget_usd=0.01` → clean stop, partial patch present, error names the
      cap. Repeat with `max_tokens_total=2000`.
- [ ] `steer_task(task_id, message)` mid-run visibly changes worker behaviour;
      `cancel_task(task_id)` afterward salvages the in-progress patch.
- [ ] Spec the worker to run `git -C . push` and `git branch -D main` → both blocked at the tool
      layer. With the allowlist disabled in a test build, confirm `git push` still fails due to
      environment neutralisation alone.

## Phase 5 — production pilot

- [ ] Pick a real repository and low-risk tasks only (tests, docs, isolated helpers).
- [ ] Run with `integrate_mode="stage"` for the first week.
- [ ] Keep `max_budget_usd ≤ 0.50` throughout the pilot.
- [ ] After the week, switch to `integrate_mode="commit"`.
