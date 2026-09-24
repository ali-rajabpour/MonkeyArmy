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
| 2 (2026-09-24) | calc toolkit: 4 modules + CLI + tests (~730 lines, 10 files) | $0.82 | ~$1.48 ($1.38 Opus + ~$0.10 workers, est.) | ~$0.10 (unpriced profile; est. at DeepSeek list prices) | 283,479 | 5 | 5 (every task first attempt) | yes — each run's suite passes against the other run's implementation; all 34 spec functions and identical error strings in both | **≈ 1.8** |

**Run 1 fails the target, and by the plugin's own rule it should have.** The brief was already a
complete spec and the code it produced was about the same size, so the code-to-spec ratio was
~1×, far below the 3× the granularity rule asks for; `assess` would have said *do it yourself*.
Opus alone needed 4 requests and 2.2k output tokens. The supervisor made 28 round trips (1.8M
cache-read tokens, 9.7k output), so its fixed overhead exceeded the whole cost of just writing
the code. This measures where delegation does **not** pay: small, fully-specified features.
Whether it pays on large mechanical work (hundreds of lines per spec) is still unmeasured.

**Run 2 raised the code-to-spec ratio to ~8× and the result improved from 2.8 to 1.8 — still a
loss.** The brief was 72 lines and produced ~730 lines across 10 files, exactly the shape this
plugin was built for, split into 5 tasks that all succeeded on the first attempt. Delegation
still cost 1.8× writing it directly.

Where it goes: the supervisor's own output nearly doubled (10.9k alone vs 18.2k delegating) and
its requests went from 8 to 19, each one re-reading the whole conversation (1.5M cache-read
tokens against 402k). Writing five specs and then *reading 732 lines of worker diff* costs more
than typing the code. Review is invariant I5 and cannot be dropped without breaking the guarantee
that nothing merges unexamined, so this is a floor, not a bug to fix by trying harder.

What delegation did win on is wall-clock: 4m 15s against 4m 40s, with five workers running in
parallel and 283k worker tokens that never touched the expensive model. The honest claim for
this tool is latency, parallelism, and keeping a large mechanical diff out of the supervisor's
context window — not dollars.

### Where run 2's money actually went

`tools/ab_cost.py` prices each supervisor turn from the session transcript (deduplicated by
`requestId`; totals reconcile with `/cost` exactly). Run it as:

```bash
python3 tools/ab_cost.py ~/.claude/projects/<slug>/<session>.jsonl 1.38
```

Run 2, delegating (19 requests, $1.38):

| Group | Turns | Supervisor output | Cost |
|---|---|---|---|
| Startup, repo reading, running tests (`Bash`) | 4 | 1.8k | $0.34 |
| Writing specs (`batch`, `dispatch_task`) | 3 | 12.8k | $0.36 |
| Pure orchestration (`configure`, `wait_for_tasks`, `review_task`, `integrate_task`) | 11 | 1.8k | $0.62 |
| Final report | 1 | 0.9k | $0.06 |

Two things fall out of this that guesswork got wrong.

**The orchestration turns are almost pure context tax.** Eleven turns produced 1.8k output
between them — they decide almost nothing — yet they cost $0.62, because each one re-bills a
conversation that grew from 54.5k to 105.7k tokens. Delegation added ~36k of permanent context
(worker diffs, tool results, specs) on top of the ~15k the direct run grew by.

**Most of the supervisor's output is retyped spec.** 12.8k of 18.2k output tokens are the micro-
specs — and in run 2 the brief on disk was already sectioned one-to-one with the five tasks, so
the supervisor was re-typing prose that already existed.

For scale, the direct run's very first turn costs $0.30 of its $0.82: writing the 50k-token
system prompt (persona, CLAUDE.md, MCP tool schemas) into cache. Both runs pay it, and no
delegation design can avoid it. That fixed floor, not the loop, is why a ratio of 0.6 was never
reachable on a feature this size.

### What would have to change for the ratio to drop below 1

1. **Spec by reference.** The supervisor currently retypes each micro-spec as output tokens. If
   `dispatch_task` accepted a file path plus a section, the worker could read the brief itself
   and the supervisor would write one line per task instead of forty.
2. **Risk-ranked review instead of full-diff review.** Have the server compute a short risk
   report (files outside the declared scope, new imports or dependencies, `subprocess`/`eval`,
   deleted or weakened tests, diff-size outliers) and let the supervisor read the full diff only
   for flagged tasks. This trades some of I5's strength for most of its cost — it needs an
   explicit decision, not a silent one.
3. **Batch the review round trip** the way dispatch is already batched: one call approving or
   rejecting several verified tasks, instead of one call per task.
4. **Bigger tasks.** Five tasks at ~150 lines each is still near the sweet spot. Fixed overhead
   per task is the dominant term, so fewer, larger tasks amortise it better — up against the
   `max_diff_lines` cap and the risk of undecided design choices.

**Shipped in 0.3.0:** `dispatch_task(spec_file=, spec_section=)`, `wait_for_tasks(require="all")`
and `review_task(reviews_json=)` — items (1), (2) and (3) above, minus the sub-agent review.

Measured against the table above, the order is: collapsing the orchestration turns is worth
about $0.45, spec-by-reference about $0.30, and sub-agent review roughly nothing in dollars once
the sub-agent's own cost is added back — its value is keeping large diffs out of the supervisor's
context. That projects to ~$0.78 against the direct run's $0.82, i.e. parity at 730 lines and
improving with size. It is a projection, not a measurement; run 3 settles it.

Until those ship, delegation is the right tool when the diff would not fit comfortably in the
supervisor's context — and the wrong tool when the only goal is a smaller bill.
