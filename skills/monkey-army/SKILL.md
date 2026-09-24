---
name: monkey-army
description: Delegate an implementation task to cheap worker models while you (an expensive frontier model) keep decomposition, specification, review and integration. Workers (DeepSeek & co. via the user's 9Router) type the code in isolated git worktrees; the monkeys MCP server verifies objectively; you approve and merge. Use when the user asks for this by name — "/monkey-army", "use monkey army for this", "unleash the monkeys", "delegate this to the monkeys", "cheap coder mode", "act as CTO" — or explicitly asks to delegate implementation work to the cheap workers. Not for one-line fixes, pure design, or debugging of unknown cause; not for work the user asked YOU to write.
---

# Monkey Army — you command, monkeys type, you sign off

You are the expensive model. Your value is judgment: decomposition, architecture, precise
specification, spotting bugs, deciding "done". Your cost is per token, and output tokens are the
expensive kind. **Never spend your tokens typing implementation code.** Break the work into
micro-tasks so small a monkey cannot get them wrong, hand each to a cheap worker through the
`monkeys` MCP tools, and review what comes back like a senior engineer signing off a junior's PR.

## 0. Profile check (first thing, every session)
Call `configure(action="status")`. If there is no default profile, or the user has not chosen one
in this session, call `configure(action="discover_models", profile=<default>)` and **ask the user
which combo/profile the monkeys should use** (and whether they want a fallback). Never change
configuration unless the user explicitly asks. If `doctor` has never run on this machine, run
`configure(action="doctor", repo_path=<repo>)` once and fix what it flags before dispatching.

## 1. Assessment mode (advice only)
If the user asks *whether* to delegate ("should we delegate this?", "assess this"), you are asked
for a verdict, NOT to run the loop. Do not decompose or dispatch. Apply:
- < ~300 changed lines in total, 1–2 files of trivial edits, or a request that is already a
  complete spec → **DO IT YOURSELF** (see §1.5 for the measurement).
- Solution path unknown (bug of unknown cause, unfamiliar code) → **DO IT YOURSELF** first, then
  reassess.
- Multiple files of mechanical or well-understood work → **DELEGATE**.
- Won't split into disjoint micro-tasks (constant rework) → lean **DO IT YOURSELF**.
- Rule of thumb: delegate a task only when the code the worker will write is at least ~3× the
  size of the spec you must write for it.
Return DELEGATE / DO IT YOURSELF / BORDERLINE with a one-line reason, then stop and wait.

## 1.5 Gate — refuse work that won't pay (mandatory, before the loop)
Running the loop has a fixed cost on YOUR side: every dispatch, wait, review and integrate is a
round trip that re-sends your whole context. Measured on this plugin: a fully-specified ~115-line
feature cost **2.8× more** delegated ($1.78 Opus + workers) than written directly ($0.65), because
that overhead exceeded the whole cost of writing the code. So, before anything else, estimate:

- **code** — total lines the workers would write for the whole request;
- **spec** — the lines of spec you would have to write (if the user's request already names every
  function, signature, message and test, the request IS the spec).

**Refuse** when any of these hold:
- code < ~300 lines in total;
- code < 3 × spec;
- the request is already a complete spec for work you could type in one pass.

Refusing means: one line with your estimate and the reason, then *"I'll implement this directly —
say 'delegate anyway' to run the monkeys regardless."* Stop there; do not decompose or dispatch.
If the user says **delegate anyway**, run the loop without re-arguing.

Delegate when the work is large and mechanical: many files, repetitive patterns, hundreds of
lines whose design is already settled — that is where your fixed overhead is spread thin.

## 2. The loop
```
ESTABLISH VERIFICATION → DECOMPOSE (batch) → DISPATCH → WAIT/SUPERVISE → REVIEW → INTEGRATE → FINISH → REPORT
```

### 2.1 Establish verification
Find the real commands first: test, lint, build (package.json scripts, Makefile, pyproject, CI).
Keep `test_command` trap-free (no globs that match nothing, no PATH surprises) and fast (< 2 min).
If the repo has no harness, the first task *is* the harness (you specify it), or you ask the user.
Read `AGENTS.md`/`CLAUDE.md` yourself once; the server injects them into worker briefs.

### 2.2 Decompose into micro-tasks
A task is small enough when: it touches **1 file** (rarely 2), changes **~20–200 lines**, has
**one acceptance command**, and every design decision is already made by you. If the worker
would have to decide anything (name, shape, dependency, approach), you under-decomposed — decide
it, bake it into the spec, shrink the task.
- Write each spec with `references/spec-template.md`: exact file, exact change, signatures/types
  inline, conventions to follow, `allowed_files`, `context_files`, acceptance.
- **Never retype a spec that already exists on disk.** If the user gave you a brief, a design doc
  or an issue file whose sections map to tasks, dispatch with `spec_file` (+ `spec_section`) and
  let the server read it. Measured: retyped spec prose was 70% of the supervisor's output tokens
  in the run-2 benchmark. If you must compose the decomposition yourself, write it once to a file
  and dispatch every task from that file's sections.
- **TDD split for logic:** task A = "write these N test cases in <test file>" (you list them),
  you review the tests; task B = "make them pass in <file>". The tests become the objective gate.
- **Prefer fewer, larger tasks.** Each task costs you ~3 round trips; don't split what one task
  can do inside ~200 lines. Skip the TDD split when the user's request already lists the tests.
- Independent tasks must have **disjoint `allowed_files`**; dependent tasks run in waves.
- Register the plan: `batch(action="create", repo_path, goal, tasks_json=[{key,title,dependsOn,allowedFiles}])`.
  Show the user the plan (the returned `order` waves) before dispatching.

### 2.3 Dispatch
For each task in the current wave, one `dispatch_task(...)` call with `batch_id` and `batch_key`;
put all independent dispatches in the same turn. Pass `spec` only for a spec you had to compose
in-conversation; otherwise `spec_file` + `spec_section`, which keeps the brief out of your
context as well as your output. A missing file or heading fails the dispatch loudly — fix it,
never fall back to retyping. Check `preflight`: a non-zero exit is normal if
tests target code that doesn't exist yet, but if the *runner* is broken (module not found,
unknown option) fix `test_command` and re-dispatch — never let a monkey fight a broken gate.
Use `mode="micro"` (default). Use `mode="task"` only for a coherent multi-file lot you
deliberately chose not to split. Do not pass `profile` unless the user asked for a specific one.

### 2.4 Wait and supervise (never idle-poll)
Call `wait_for_tasks(task_ids, include_results=True, require="all")` — it returns once the whole
wave is finished, with every task's full result (verification, scope, diffstat, patch) attached.
Use `require="any"` only when you genuinely need to act on the first finisher. Each wake costs a
round trip that re-sends your whole context, so waking five times for five tasks is five times
the tax for no extra information.
- `needs_input`: read the question. Answer from your own context with `answer_worker` when it is
  an implementation detail you already decided; relay to the user only genuine product decisions.
  Answer promptly — the worker is blocked.
- Mid-course correction (you or the user spot something): `steer_task(task_id, message)`.
- Stalled or rogue: `cancel_task(task_id)` (its work is salvaged for review).
- Use `task_progress` only when something looks stuck; never read raw logs.

### 2.5 Review — you do it, never a subagent
On `done`, review the `result` the wait returned — call `task_result` only if it is missing.
Then, in this order:
1. `verification.passed` must be true and `scope.ok` must be true. If not, the status tells you
   why (`failed_verification`, `failed_scope`, `failed_oversized`); the patch is still there.
2. Read the patch (inlined when small; otherwise open `patch_path`) against
   `references/review-checklist.md`: does it do exactly what you specified, in the repo's style,
   with no silent fallbacks, swallowed errors, scope creep, dead code, or misleading names? Are the
   tests meaningful? Would you have written it this way?
3. Judge: `review_task(task_id, "approve", integrate=True)` — approves and merges in one call —
   or `review_task(task_id, "reject", feedback)` with
   the **exact** problem, the root cause, and the fix you want. Reviewed a whole wave? Send the
   verdicts in one call: `review_task(reviews_json='[{"task_id": ..., "verdict": "approve"}, ...]',
   integrate=True)`. Read every diff first — batching the *call* is not batching the *reading*. You diagnose; the monkey retypes in
   the same worktree. After two rejects, stop: do it yourself or re-decompose.
Never trust `summary`. Never approve on "tests pass" alone — read the diff.

### 2.6 Integrate
Approving with `integrate=True` already merged it. Use `integrate_task(task_id)` only for a task
approved without it, or with `mode="stage"` (default mode from config: `commit` = one
squash commit on the user's current branch; `stage` = leave staged for the user to commit).
Integrate in dependency order. On `conflict` (base moved), do not hand-resolve: re-dispatch that
task against the current branch and review it again — micro-tasks are cheap. The worker's
worktree and branch are removed on integrate; nothing lingers.

### 2.7 Finish
`batch(action="finish", batch_id, verify_command=<full test suite>)` integrates any remaining
approved tasks, runs the whole suite once in the user's tree, and asserts the end state: one
worktree, no `monkey/*` branches. If the final verify fails, tell the user exactly which tests and
propose a fix task; never leave it unmentioned.

### 2.8 Report
One compact table: task, attempts, worker cost, lines added/removed, verification. State the
total worker cost and remind the user to compare with `/cost` for your side. Note anything you
added to the repo notes (`configure(action="add_note", …)`) for future runs — one line each,
only durable operational facts ("tests need `uv run pytest -q`").

## 3. Rules
- While the loop runs, one short line per step. Narration is output tokens — the expensive kind.
- Never write implementation code yourself, except ≤ 20-line glue that would cost more to specify.
- Never dispatch a task that requires a design decision; make the decision first.
- Never trust the worker's summary or claimed success; the server's `verification` and your
  reading of the diff decide.
- Never approve an unread patch; never integrate an unapproved task (the server refuses anyway).
- Never call mutating `configure` actions unless the user asked in this conversation; never
  switch profiles as a reaction to a failing task — report and let the user decide.
- Never read `.jsonl` logs or worktree files directly; use `task_result`/`task_progress`.
- Never leave worktrees or `monkey/*` branches behind; `finish` or `cleanup_task` every task.
- Workers never push; you never push either — the user owns `git push`.
- Keep your own messages to the worker short; keep the spec precise instead.

## 4. Common mistakes
- Writing the code yourself "because it's faster" — that is the exact cost you are avoiding.
- Polling `task_status` in a loop instead of `wait_for_tasks`.
- Waking once per task (`require="any"`) when the whole wave has to finish anyway.
- Retyping a spec that is already a file on disk instead of passing `spec_file`.
- Specs that make the worker explore ("find where X is handled") — you name the file and line.
- Tasks that touch many files, or two parallel tasks sharing a file.
- Approving because tests are green without reading the diff.
- Delegating review or verification to another cheap agent.
- Serialising independent tasks.

## 5. Cost model, honestly
You spend expensive tokens on decomposition and review and push implementation volume onto cheap
workers — but two A/B runs measured delegation costing **more**, not less: 2.8x on a 115-line
feature, 1.8x on 730 lines across 10 files (`docs/TOKEN-ECONOMICS.md`). The reason is structural:
your own turns re-send your whole context, and a run's first turn alone (loading the system
prompt) is about a third of what writing the feature directly costs.

So do not promise the user savings. What delegation reliably buys is a large mechanical diff that
never has to fit in your context, worker tokens that are not yours, and parallel execution. Say
exactly that when you assess, and if the user's goal is a smaller bill on a small feature, tell
them to let you write it.
