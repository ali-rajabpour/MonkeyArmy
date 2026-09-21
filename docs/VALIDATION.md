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

- [x] `curl -sS "$BASE/models" -H "Authorization: Bearer $KEY"` lists the combo.
  **Result (2026-09-21, live):** 107 models listed, incl. `coder`, `coder-fast`, `daily` (this router lists combos unprefixed).
- [x] One `chat/completions` call with a `tools` array to `combo/<id>` returns `tool_calls` and
      `usage`; note the `model` field in the response.
  **Result (2026-09-21, live):** probe `ok`, `tool_calling: confirmed`, `usage_present: true`, model `deepseek-flash`, 1061 ms.
- [x] `uv --version` and `git --version` both succeed.
  **Result (2026-09-21, live):** doctor: all 11 checks green (uv, git, python, config, default profile, key, probe, models endpoint, worker selftest, orphans, notes).
- [x] `python3 -m unittest discover -s server/tests` is green.
  **Result (2026-09-21, live):** 229 tests OK.
- [x] `uv run worker/tests/test_worker.py` is green.
  **Result (2026-09-21, live):** 37 tests OK.
- [x] 9Router dashboard: RTK / Caveman tool-result compression is OFF for the worker key.
  **Result (2026-09-21, live):** Confirmed by T1.4: a 500-byte mixed-unicode fixture round-tripped and `cmp` reported no differences.

## Phase 1 — plumbing (after WP3/WP4)

- [x] T1.1: `configure(action="doctor")` is all green; `configure(action="discover_models",
      profile=<default>)` lists the combo; `configure(action="probe", profile=<default>)`
      returns `tool_calling: "confirmed"`.
  **Result (2026-09-21, live):** doctor 11/11; `last_doctor_at` recorded and surfaced on `status`; discover_models finds `coder`; probe confirmed.
- [x] T1.2 (TDD split — the toy suite has no `subtract` test yet, so there is nothing to fail
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
  **Result (2026-09-21, live):** task A `mk_mubl0vvz_woopgq` (tests) and task B `mk_mubl1atj_yy6pd9` (implementation) both succeeded, verified, approved and integrated; B `priced: true`, `models_seen: ["deepseek-flash"]`.
- [x] T1.3: during the run, `git status` inside `/tmp/toy` stays clean, and the worktree appears
      under `~/.monkey-army/repos/<slug>/worktrees/<task_id>/`, not inside `/tmp/toy`.
  **Result (2026-09-21, live):** user repo clean right after dispatch and after integration; worktree under `MONKEY_ARMY_HOME`.
- [x] T1.4 (compression check): dispatch a task that prints `fixtures/sentinel.txt` with `cat`
      and copies it verbatim into `fixtures/copy.txt`; inside the worktree,
      `cmp fixtures/sentinel.txt fixtures/copy.txt` reports no differences.
  **Result (2026-09-21, live):** `mk_mubl1m0z_285khw` succeeded; `cmp` reports no differences.
- [x] T1.5: dispatch with a temp profile pointing at a wrong model name → `probe` fails and
      `dispatch_task` refuses before any worktree is created.
  **Result (2026-09-21, live):** bad model → probe `HTTP 404 model_not_found`; dispatch refused; worktrees before=1 after=1.

## Phase 2 — gates and merge-back (after WP5)

- [ ] T2.1: dispatch a task whose worker edits a file outside `allowed_files` → job ends
      `failed_scope`; `/tmp/toy` main tree untouched.
  **Result (2026-09-21, live):** SKIP live — the model stayed inside `allowed_files` (`succeeded`); a well-behaved model cannot be forced out of scope from outside. Main tree untouched. Scope enforcement is covered by unit tests, and fired live when the harness itself left a stray file in a worktree (`failed_scope`, fixed in the harness).
- [x] T2.2: dispatch a spec that instructs the worker to "skip the tests" against a repo with a
      failing test → job ends `failed_verification` (server re-ran the test itself).
  **Result (2026-09-21, live):** `mk_mubl699o_u8l70g` → `failed_verification` (`verify exit 1`); the gate is a server-side `verify_command`, the worker's own task was satisfiable.
- [x] T2.3: dispatch two parallel tasks with disjoint `allowed_files`, approve both, run
      `batch(action="finish", ...)` → one worktree left, zero `monkey/*` branches, two commits on
      `main`, `pytest` green.
  **Result (2026-09-21, live):** batch `b_mubl87xq_1ulr`, tasks `mk_mubl87y6_dqrwoo` + `mk_mubl889s_9wfs5r`: both succeeded and approved; `finish` without `repo_path` integrated both (commits 4→6); `worktreesLeft: 0, branchesLeft: 0`; suite green; cost $0.017.
- [x] T2.4: dispatch two tasks that both touch the same file; integrate the first, then attempt
      `integrate_task` on the second → `integrated:false, reason:"conflict"`, tree left clean,
      branch preserved; re-dispatch the second task against the updated `main` and confirm it now
      integrates.
  **Result (2026-09-21, live):** c1 `mk_mubl8mw5_1n01kl` integrated; c2 `mk_mubl8n7u_rkt6iq` → `conflict`, HEAD unchanged, branch preserved; re-dispatch against the moved base integrated.
- [x] T2.5: `integrate_task(task_id, mode="stage")` → changes staged (not committed), worktree
      and branch cleaned up.
  **Result (2026-09-21, live):** `mk_mubl997y_188nzv` integrated with `mode=stage`: `A calc/staged.py` staged, no new commit, worktree cleaned.
- [x] T2.6: restart Claude Code mid-task → `task_status(task_id)` still resolves correctly after
      restart; `cancel_task(task_id)` works on the now-orphaned job; salvaged patch is present.
  **Result (2026-09-21, live):** `mk_mubl9kl9_98z5ty`: server killed and relaunched mid-task; `task_status` resolves; `cancel_task` → `cancelled`, `salvaged: true`, patch present.
- [x] T2.7: dispatch a task engineered to produce > 300 changed lines → job ends
      `failed_oversized`.
  **Result (2026-09-21, live):** `failed_oversized` returned for a >300-line diff.
- [x] T2.8: dispatch a task whose worker is expected to ask a clarifying question →
      `wait_for_tasks` returns early with the question; `answer_worker(task_id, answer)` resumes
      it and the stall clock resets.
  **Result (2026-09-21, live):** `mk_mubl9yxj_hcelsr`: `wait_for_tasks` returned early (7 s) on `needs_input`; `answer_worker` delivered; task resumed and succeeded.
- [x] T2.9: `review_task(task_id, "reject", feedback="<specific problem>")` → attempt 2 runs in
      the same worktree; the final diff includes both attempts' changes; `integrate_task`
      produces one squash commit covering both.
  **Result (2026-09-21, live):** `mk_mubl699o_u8l70g` rejected → attempt 2 in the same worktree → succeeded → integrated as one squash commit covering both attempts (commits 2→3).

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
  **Result (2026-09-21, live):** Not drilled deterministically. Partly covered: T4.1 refuses dispatch to an unreachable endpoint with nothing left behind, and real router outages during this session were refused at the probe gate the same way. An outage *during* a worker run is bounded by the per-request timeout and the stall watchdog.
- [x] Dispatch with a broken `test_command` (e.g. wrong test runner flag) → the preflight note
      surfaces the broken command instead of the job proceeding silently.
  **Result (2026-09-21, live):** preflight note surfaced (`exit_code: 1`).
- [x] Dispatch with `max_budget_usd=0.01` → clean stop, partial patch present, error names the
      cap. Repeat with `max_tokens_total=2000`.
  **Result (2026-09-21, live):** USD cap: `budget exceeded: cost $0.0016 crossed the … USD cap`. Token cap: `5915 tokens crossed the 1 token cap`. Both stop cleanly and name the cap.
- [x] `steer_task(task_id, message)` mid-run visibly changes worker behaviour;
      `cancel_task(task_id)` afterward salvages the in-progress patch.
  **Result (2026-09-21, live):** `mk_muble39j_iyj81s`: steering visibly changed the worker's output; `mk_mublebwj_odgqt1`: `cancel_task` → `cancelled`, `salvaged: true`.
- [x] Spec the worker to run `git -C . push` and `git branch -D main` → both blocked at the tool
      layer. With the allowlist disabled in a test build, confirm `git push` still fails due to
      environment neutralisation alone.
  **Result (2026-09-21, live):** blocked at the tool layer; task continued normally.

## Phase 5 — production pilot

- [ ] Pick a real repository and low-risk tasks only (tests, docs, isolated helpers).
- [ ] Run with `integrate_mode="stage"` for the first week.
- [ ] Keep `max_budget_usd ≤ 0.50` throughout the pilot.
- [ ] After the week, switch to `integrate_mode="commit"`.
