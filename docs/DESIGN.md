# Design

## Purpose

A Claude Code plugin where Claude Code on an Opus-class model (the *supervisor*, talking to
Anthropic normally, never re-routed) plans, decomposes, specifies, supervises, reviews and
integrates, while cheap workers (DeepSeek and other models reached through the user's local
9Router) do the execution-heavy typing. The goal is fewer tokens for the same final quality:
the supervisor makes every design decision up front, the server verifies objectively, and the
supervisor is the only party that can unlock integration.

## Architecture

```
                    Claude Code (Opus-class supervisor)
                                  |
                       monkeys MCP server (FastMCP)
   dispatch_task   wait_for_tasks   task_status     task_progress
   answer_worker   steer_task       cancel_task     task_result
   review_task     integrate_task   batch           cleanup_task
   configure
                                  |
        +-------------------------+--------------------------+
        |                         |                          |
   jobs.py / git_ops.py      verify.py                worker_launcher.py
   (worktree lifecycle,      (scope check,             (spawn worker.py,
    staging, commit,          post-run verify,          env filtering,
    integrate, cleanup)       diff-size cap)             brief, watchdog)
        |                                                     |
  ~/.monkey-army/repos/<slug>/                        worker.py
    worktrees/<task_id>/  <- git worktree,          (deepagents + litellm)
                             branch monkey/<task_id>          |
                                                     9Router (OpenAI-compatible)
                                                     model: combo/<id> (DeepSeek & co.)
```

The user's repository itself is never written to directly by a worker; all worker state lives
under `~/.monkey-army/`, outside the repo (see Data model).

## Invariants

I1. The supervisor session's provider configuration is never touched. The plugin sets no
    `ANTHROPIC_*` variables and writes nothing into the Claude Code settings besides what the
    user opts into (status line).
I2. Workers never write to the user's working tree. All worker writes happen in a disposable git
    worktree stored outside the repository (`~/.monkey-army/repos/<slug>/worktrees/<task>`).
I3. Workers cannot push, fetch, merge, rebase, alter shared refs (branches, tags, stash, reflog),
    or use the user's git credentials. Enforced at the tool layer and by environment, not only
    by prompt.
I4. "Succeeded" is decided by the server re-running the acceptance command(s) in the worktree
    and checking scope and diff size — never by the worker's or an LLM grader's claim.
I5. Integration into the user's branch requires `status == succeeded` and an explicit
    `review_task(verdict="approve")` recorded by the supervisor. There is no path around it.
I6. Integration never leaves the user's tree in a half-merged state: it dry-runs first
    (`git apply --check`) and either applies fully or reports a conflict having changed nothing.
I7. Every worker run has hard limits: recursion, wall-clock, stall, per-command timeout, USD
    budget and a token budget (so the cap works even when pricing is unknown).
I8. Secrets never enter the model conversation (elicitation for keys), never reach worker shell
    commands (env filtering), and the worker process receives only its own provider key.
I9. Configuration changes happen only on explicit user request (skill rule + tool descriptions).
I10. Licensing & attribution: MIT; `NOTICE` credits cc-delegate and third-party components.

## Data model

State lives entirely under `~/.monkey-army/` (override `MONKEY_ARMY_HOME`), keyed by
`slug = f"{basename(repo)}-{sha1(abs_repo_path).hexdigest()[:8]}"`. Nothing is ever written
inside the user's repository. Per-repo: `notes.md`, `batches/<id>.json`, `jobs/<task_id>.json`,
`logs/<task_id>.jsonl`, `comm/<task_id>/`, `patches/<task_id>.diff`,
`worktrees/<task_id>/` (the actual git worktree, branch `monkey/<task_id>`). Global:
`config.json` (profiles + defaults, no secrets), `credentials.json` (mode 0600), `repos.json`
(slug → absolute repo path, for restart lookup), `statusline`.

A job record carries everything about one task attempt: spec, scope (`allowedFiles`),
model/profile, status, review verdict, verification results, scope/diffstat, patch location,
cost and token counts, and timestamps. A batch manifest links task keys to task ids and
dependency order, and accumulates a report on finish.

## Task lifecycle

```
running --(worker done)--> verifying --> succeeded | failed_scope | failed_oversized | failed_verification
running --(worker asks)--> needs_input --(answer_worker)--> running
running --(no progress)--> timeout | cancelled
any TERMINAL --(review_task)--> reviewed (verdict recorded on the job)
succeeded + approved --(integrate_task)--> integrated
```

`verifying` and `needs_input` are transient states the server sets; only the server moves a job
into `succeeded`/`failed_*` (§ Verification pipeline). Only the supervisor moves a job through
`review_task` and `integrate_task` — no other path reaches `integrated`.

## Verification pipeline

After the worker exits (or is salvaged on failure), the server — not the worker, not an LLM
grader — runs a fixed pipeline in the worktree: compute changed files, check them against
`allowedFiles` (scope), stage the in-scope ones, diff against the base commit, reject if the
diff exceeds the configured line cap, then run `testCommand`, `verifyCommand`, `lintCommand` in
that order, stopping at the first failure. Only if every step passes does the job become
`succeeded` and get committed in the worktree.

deepagents ships a `RubricMiddleware` that can have an LLM grade a task against a rubric. This
plugin does not use it as the pass/fail gate in `mode="micro"`: an LLM verdict is a claim, not a
fact, and invariant I4 requires an objective re-run. The rubric machinery is available for
`mode="task"` internally but never substitutes for the acceptance command; a worker's own
claimed status is recorded for the record (`workerClaimedStatus`) but never decides `job.status`.

## Integration algorithm

`integrate_task` requires `status == succeeded` and a recorded `approve` verdict, then checks
there is no in-progress git operation and no dirty overlap with the files the task touched. It
builds a fresh patch (`git diff --binary --no-renames <baseSha> <branch>`, covering every attempt's commits),
dry-runs it with a strict `git apply --index --check`, and only if that succeeds applies it for
real with `git apply --index`. On `mode="commit"` it commits with the user's own git identity;
on `mode="stage"` it leaves the changes staged. Either way the worktree and `monkey/*` branch
are removed immediately after.

`git apply --check` was chosen over `git merge --squash` because it is stateless: a failed dry
run modifies nothing (needed for I6), while a squash merge touches the index and can leave the
tree in a half-applied state that needs an explicit abort. It also avoids putting a real merge
commit or `ORIG_HEAD` history into the user's repo for what is, from the user's point of view,
one atomic change.

Note: `--3way` is deliberately **not** used. `git apply --index --check --3way` exits 0 even
when the 3-way apply would produce conflicts (it reports "Applied patch ... with conflicts" but
still returns success), and applying with `--3way` then leaves `UU` conflict markers in the
tree — which violates I6 (dry run must correctly predict the real apply, and a real apply must
never leave the tree in a conflicted state). The strict (non-`--3way`) form correctly exits
non-zero on `error: patch failed: <path>:<line>` / `error: <path>: patch does not apply`, so
that is what both the dry run and the real apply use.

## Security model

Enforced at the **tool layer** (workers cannot execute it in the first place): a git-command
allowlist restricts workers to read-only and worktree-local subcommands (`status diff log show
add blame grep ls-files rev-parse apply rm mv restore`, restricted `branch`/`reset`, `commit`);
everything that touches shared refs or credentials (`push fetch pull merge rebase stash tag
remote worktree checkout switch clone` etc.) is blocked with an explanation. The allowlist also
catches wrappers and indirection: `env git ...`, `xargs git ...`, combined shell invocations
(`sh -lc "..."`), and `git` reached through `$(...)` or backtick command substitution. It fails
closed — a quoted argument that merely *mentions* `git` alongside whitespace gets re-checked as
if it might be a command, so a benign case like `grep "git push"` is blocked too. That is an
accepted false positive: refusing a safe command is cheap, missing a dangerous one is not.

Enforced by **environment** (belt and braces even if the tool-layer check were bypassed): the
launcher injects a `GIT_CONFIG_*` block that rewrites every remote's `url` and `pushurl` to an
unreachable `monkey-army-blocked://` scheme, sets `credential.helper` empty and `core.askPass`
to `false`, and sets `GIT_TERMINAL_PROMPT=0`. `SSH_AUTH_SOCK` is dropped and
`GIT_SSH_COMMAND=false` is set, so a push to an explicit SSH URL (which bypasses the remote
overrides) has no agent and no ssh to use either. Inherited `GIT_CONFIG_*` variables are
removed first so stale indices cannot survive. Environment secret
filtering ensures the worker process only ever sees its own provider key, never the
supervisor's or any other credential lying around in the parent environment.

Enforced **server-side**: the verification pipeline above re-runs the acceptance commands
itself rather than trusting the worker's report, and integration re-validates preconditions
immediately before touching the user's tree.

Honest gap: shell commands the worker runs inside its worktree are **not sandboxed** beyond the
above. There is no container, no seccomp profile, no filesystem jail — a worker that finds a way
around the git allowlist (e.g. through a completely unrelated destructive shell command) is only
stopped by whatever the git-specific and environment-specific controls above happen to cover.
Treat the worktree as attacker-adjacent, not attacker-proof; that's the honest boundary this
design draws today (see Roadmap for a container sandbox).

## Token economics

See [`TOKEN-ECONOMICS.md`](TOKEN-ECONOMICS.md) for the granularity rule, what's enforced vs.
advised, and the measurement protocol.

## Known upstream caveats

### deepagents `LocalShellBackend` timeout can hang forever on Windows

**Status:** worked around in `worker/worker.py` (`SupervisedShellBackend`), not fixed upstream.

The stock backend runs commands with `subprocess.run(shell=True, timeout=...)`. On Windows,
CPython's `TimeoutExpired` path kills only the direct shell process and then calls
`communicate()` again **without a timeout** to collect output. If the killed shell had spawned
a grandchild (e.g. `bash → find`), the grandchild survives, keeps the inherited stdout pipe
open, and that second `communicate()` blocks forever — one stuck command freezes the whole
agent loop. Our backend runs the process itself and kills the entire process tree
(`taskkill /F /T` / POSIX process groups) on expiry.

### deepagents `virtual_mode` silently remaps absolute paths

**Status:** mitigated via the worker's system prompt (relative paths mandated), not fixed.

With `virtual_mode=True`, a file operation on an absolute path (`/c/Users/.../file.txt` or
`C:/...`) is interpreted as virtual-root-relative and lands at `<worktree>/c/Users/.../file.txt`
— silently misplaced, no error. Models produce absolute paths spontaneously (often echoing
`pwd` output), so greenfield files can end up in a junk subtree. Reproduced deterministically
with deepagents 0.7.0a6.

## Decisions

2026-09-19:
- deepagents used as a library (not forked) for the worker's agent loop and shell backend.
- litellm used as the provider layer so any OpenAI-compatible endpoint (9Router) works without
  a bespoke client.
- State lives outside the repository, under `~/.monkey-army/`, keyed by a slug derived from the
  repo path — never inside the user's working tree.
- No OAuth in the worker: 9Router owns subscription/provider auth; the worker only ever holds a
  plain API key for its own profile.
- `wait_for_tasks` (blocking with early wakeup on state change) instead of the supervisor
  polling `task_status` in a loop — each poll turn re-sends the supervisor's whole context, so
  polling is the expensive choice.
- Pinned versions: `deepagents==0.7.15`, `langchain-litellm==0.7.2`, `litellm==1.101.0`,
  `fastapi==0.116.1` (transitively `langchain-core 1.6.3`, `langgraph 1.2.11`).
- `mcp` pinned `<2`: the 2.x line renamed `FastMCP`, which this server's registration code
  depends on directly.
- Integration dry-run and apply use strict `git apply --index --check` / `git apply --index`,
  **without** `--3way` — verified that `--3way`'s check exits 0 even for a conflicting apply,
  which would violate I6 (see Integration algorithm above).
- `validate_model_string` requires a `provider/model` shaped string (e.g. `openai/combo/<id>`);
  a bare model name is rejected up front rather than failing opaquely inside litellm.
- `repos.json` I/O lives in `persistence.py`, not in `store.py` or `jobs.py`: both of those
  modules need to resolve a slug to a repo path, and putting the I/O in either one would create
  an import cycle between them; `persistence.py` sits underneath both.
- The git command allowlist also checks wrappers (`env git`, `xargs git`), combined shell
  invocations (`sh -lc "..."`), and `$(...)`/backtick substitution, and fails closed: a quoted
  argument that merely mentions `git` next to whitespace is re-checked as a possible command, so
  `grep "git push"` is blocked too. Accepted as a false positive — refusing a safe command costs
  a retry; missing a dangerous one costs the invariant.
- The integration patch is built with `--no-renames`, its paths are read with
  `git apply --numstat -z` before applying, and the squash commit uses
  `git commit -- <those paths>`. Otherwise a plain `git commit` would sweep anything the user
  had already staged into the monkey commit. `diff_and_stat` uses `--no-renames` too, so a
  rename's source path is never missed by the scope or dirty-overlap checks.
- Conflict files are parsed from three stderr forms: `patch failed: <path>:<line>`,
  `<path>: patch does not apply`, and `<path>: already exists in index/working directory`
  (new-file adds; not named in the plan, found by the conflict test).
- The minimal `integrated` job record keeps cost, tokens, model, diffstat totals and the review,
  so batch reports still total correctly after the worktree and full record are gone.
- A rejected task's brief keeps one "Supervisor feedback on attempt N" section per past attempt
  (`feedbackHistory`), not only the latest.
- The probe strips the litellm provider prefix up to the first `/` (`openai/combo/x` →
  `combo/x`) for the raw HTTP call; the worker passes the full string to `ChatLiteLLM`, and
  litellm strips it itself (verified with `litellm.get_llm_provider`).
- `task_result` replaced the old `fetch_task_result` outright instead of coexisting with it.
- `--worktree`/`--brief` are optional in the worker's argparse (checked manually after the
  `--selftest` branch) so `worker.py --selftest` runs without them.

## §6 tool count note

§6 of the implementation plan is titled "MCP tool surface (exactly these 12 ...)" but its own
subsections 6.1–6.13 define 13 distinct tools (`dispatch_task`, `wait_for_tasks`, `task_status`,
`task_progress`, `answer_worker`, `steer_task`, `cancel_task`, `task_result`, `review_task`,
`integrate_task`, `batch`, `cleanup_task`, `configure`). All 13 are implemented; the heading
count is stale and documented here rather than silently dropping a tool to match it.

## Lineage

Derived from cc-delegate by Etienne Lescot, MIT licensed. See `NOTICE` for full attribution.
