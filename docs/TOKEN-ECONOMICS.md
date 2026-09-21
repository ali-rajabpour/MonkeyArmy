# Token economics

## The granularity rule

Delegate a task only when the code a worker will write is at least **~3× the size of the spec**
you (the supervisor) must write for it. Below that ratio, writing the spec costs as much as
writing the code — you're paying expensive tokens either way, so write the code yourself.

## The sweet spot: 20–200 changed lines

A task is well-sized when it touches ~1 file (rarely 2), changes roughly **20–200 lines**, has
one acceptance command, and every design decision is already made before dispatch. Below ~20
lines, the spec-to-code ratio breaks the granularity rule. Above ~200 lines (or the configured
`max_diff_lines` cap, default 300), the worker is more likely to make an undecided design choice
along the way, which forces a reject-and-retype cycle — split the task instead.

## Polling is the hidden cost

Every `task_status` poll from the supervisor re-sends the supervisor's entire conversation
context to an expensive model — the poll itself costs more than the answer it returns. This is
why `wait_for_tasks` exists: it blocks server-side and wakes the supervisor only on a real state
change (done, needs input, or timeout), turning what would be N expensive round trips into one.

## What's enforced vs. what's advised

| Lever | Enforced by | Advisory only |
|---|---|---|
| Acceptance (tests/verify/lint pass) | Server re-runs the commands (§ Verification pipeline) | — |
| File scope (`allowed_files`) | Server compares changed files against the glob list | — |
| Diff size (`max_diff_lines`) | Server rejects (`failed_oversized`) before verification | — |
| Cost cap (`max_budget_usd`) | Worker stops itself once tracked spend exceeds the cap | Accuracy depends on pricing being known (`priced`) |
| Token cap (`max_tokens_total`) | Worker stops itself; works even when pricing is unknown | — |
| Wall-clock / stall / per-command timeout | Launcher watchdog kills the process | — |
| Task granularity (20–200 lines, 1 file) | Nothing — it's a rule of thumb for the supervisor | Entirely advisory; the caps above are the backstop, not this rule |
| "Worker followed the repo's conventions/style" | Nothing automated | Supervisor review of the diff is the only check |
| Micro-task decomposition quality | Nothing automated | Supervisor judgment during decompose (§2.2 of the skill) |

The objective gates (acceptance, scope, diff size, budget, tokens, time) are what make "succeeded"
mean something without trusting the worker's or an LLM grader's claim (invariant I4). Everything
in the advisory column is exactly where supervisor judgment is still required — that's the part
of the job the supervisor is not allowed to delegate.

## A/B measurement protocol

To measure actual savings on your own workload (validation plan Phase 3): pick one real feature,
sized at roughly 5 micro-tasks / ~150 changed lines. Implement it twice from fresh Claude Code
sessions:

- **(A) Opus alone** — no delegation, normal editing.
- **(B) `/monkey-army`** — full assess → decompose → dispatch → review → integrate loop.

For both, record the total cost from `/cost`. For (B) also record the batch report (worker cost,
worker tokens, per-task attempts). Confirm both produce equivalent passing tests, and do a quick
side-by-side quality read of the two diffs — savings that come with a quality drop don't count.

Target: total cost of (B) ≤ 0.6× total cost of (A), with equal quality. If you don't hit that,
the tasks were probably too small for the granularity rule above — widen them and re-run before
trusting the number.

## Running it: the calc benchmark

A ready-made feature for the protocol above, sized to the sweet spot and fully specified so both
runs build the same thing. About 150 changed lines, splitting naturally into ~5 tasks.

### 1. Two identical starting points

```bash
for run in A B; do
  rm -rf /tmp/ab-$run
  cp -r examples/toy-repo /tmp/ab-$run
  (cd /tmp/ab-$run && git init -q -b main && git add . && git commit -qm init)
done
```

### 2. Fairness rules

- **Fresh session for each run**: `cd /tmp/ab-A && cc-coder` (not `--resume` / `--continue`).
- **Same persona, model and effort** for both. The persona and its tools cost tokens in both runs
  equally; changing it between runs breaks the comparison. `cc-coder` is the one with the
  monkeys tools enabled.
- **Paste the prompt, then stay out of it.** Answer questions if asked; don't steer, don't review
  the code early, don't paste extra context. Every message you add is cost the run didn't need.
- **Measure before you read the code.** Type `/cost` the moment the run says it's done.

### 3. The feature brief (identical in both prompts)

```text
Feature: extend the calc package with four operations.

Library — calc/__init__.py, matching the existing style (plain functions, one-line docstrings):
- subtract(a, b) returns a - b
- divide(a, b) returns a / b (true division, a float). If b == 0, raise ValueError("division by zero").
- power(a, b) returns a ** b
- modulo(a, b) returns a % b. If b == 0, raise ValueError("modulo by zero").

CLI — calc/cli.py: add subcommands subtract, divide, power and modulo. Each takes two int
arguments exactly like the existing add and multiply, and prints the result. If the operation
raises ValueError, print "error: <message>" to stderr and have main() return 2.

Tests — tests/test_calc.py:
- one normal-case test per new function;
- for divide and modulo, a test that b == 0 raises ValueError with the exact message;
- one CLI test per new subcommand checking the printed output;
- one CLI test that `divide 1 0` prints the error to stderr and returns 2.

Acceptance: `uv run --no-project --with pytest python -m pytest -q` passes, including the
existing tests.

Constraints: no new dependencies; do not change add, multiply or their tests; do not reformat
unrelated code.
```

### 4. Prompt A — Opus alone (run in `/tmp/ab-A`)

```text
Implement the following feature yourself, directly in this repository. Do not delegate: do not
use monkey-army, subagents, or any worker tools. Run the acceptance command until it passes,
then stop and tell me you are done.

<paste the feature brief here>
```

### 5. Prompt B — monkey-army (run in `/tmp/ab-B`)

```text
/monkey-army:monkey-army Implement the following feature. Use the full monkey-army loop for all
implementation work, integrate with mode=commit, and when the batch is finished show me the batch
report (worker cost and tokens included).

<paste the feature brief here>
```

### 6. Record

In each session, as soon as it reports done:

```text
/cost
```

Then, from any terminal:

```bash
# both must pass
for run in A B; do (cd /tmp/ab-$run && uv run --no-project --with pytest python -m pytest -q | tail -1); done

# the two diffs, side by side, for the quality read
for run in A B; do echo "=== $run"; git -C /tmp/ab-$run diff --stat $(git -C /tmp/ab-$run rev-list --max-parents=0 HEAD); done
git -C /tmp/ab-A diff $(git -C /tmp/ab-A rev-list --max-parents=0 HEAD) > /tmp/ab-A.diff
git -C /tmp/ab-B diff $(git -C /tmp/ab-B rev-list --max-parents=0 HEAD) > /tmp/ab-B.diff
```

For B, **total cost = session B's `/cost` + `workerCostUsd` from the batch report** — the
supervisor alone is not the whole bill. Put both runs in the table below.

On a subscription plan `/cost` shows what the tokens would cost rather than what you are billed.
That is fine here: the comparison is the ratio B / A, not the absolute amount.

**Reading the result:** B / A ≤ 0.6 with equal quality means delegation pays on this kind of
work. Above that, look at the batch report first — many attempts per task, or tasks of a few
lines each, mean the tasks were cut too small; re-cut them larger and run B again before
trusting the number.

## Results

To be filled from the user's A/B run — no numbers fabricated.

| Run | Feature | Total cost (A, Opus alone) | Total cost (B, monkey-army) | Worker cost | Worker tokens | Tasks | Attempts (total) | Quality equivalent? | B / A ratio |
|---|---|---|---|---|---|---|---|---|---|
| 1 (2026-09-22) | calc: 4 ops + CLI + tests (~115 lines) | $0.65 | ~$1.85 ($1.78 Opus + ~$0.07 workers, est.) | ~$0.07 (unpriced profile; est. at DeepSeek list prices) | 202,075 | 3 | 4 (one re-dispatch after a supervisor-written verify command failed) | yes — 15/15 tests both; add/multiply untouched | **≈ 2.8** |

**Run 1 fails the target, and by the plugin's own rule it should have.** The brief was already a
complete spec and the code it produced was about the same size, so the code-to-spec ratio was
~1×, far below the 3× the granularity rule asks for; `assess` would have said *do it yourself*.
Opus alone needed 4 requests and 2.2k output tokens. The supervisor made 28 round trips (1.8M
cache-read tokens, 9.7k output), so its fixed overhead exceeded the whole cost of just writing
the code. This measures where delegation does **not** pay: small, fully-specified features.
Whether it pays on large mechanical work (hundreds of lines per spec) is still unmeasured.
