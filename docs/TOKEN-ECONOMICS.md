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

## Results

To be filled from the user's A/B run — no numbers fabricated.

| Run | Feature | Total cost (A, Opus alone) | Total cost (B, monkey-army) | Worker cost | Worker tokens | Tasks | Attempts (total) | Quality equivalent? | B / A ratio |
|---|---|---|---|---|---|---|---|---|---|
| | | | | | | | | | |
