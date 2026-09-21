# Changelog

## 0.1.0 — 2026-09-21

First release. Forked from cc-delegate by Etienne Lescot (MIT) and rebuilt as Monkey Army.

### What it does

An expensive supervisor model decomposes work, specifies it precisely, reviews every patch and
merges it. Cheap workers (DeepSeek and friends, through a local 9Router) do the typing, each in
its own disposable git worktree outside your repository. The server — not the worker, not an LLM
grader — decides whether a task succeeded.

### Added

- **13 MCP tools**: `dispatch_task`, `wait_for_tasks`, `task_status`, `task_progress`,
  `answer_worker`, `steer_task`, `cancel_task`, `task_result`, `review_task`, `integrate_task`,
  `batch`, `cleanup_task`, `configure`.
- **Skills**: `monkey-army` (the delegation loop), `assess` (delegate or not — advice only),
  `setup` (guided configuration and repair).
- **Commands**: `/monkey-army:setup`, `:status`, `:repair`, `:reset`.
- **Server-side verification**: the acceptance command is re-run by the server, scope is checked
  against `allowed_files` (including files the worker committed), the diff is capped, and the
  branch must still descend from its base.
- **Review gate**: integration requires `status == succeeded` *and* a recorded `approve`. Rejects
  re-run the worker in the same worktree with your feedback, up to three attempts.
- **Integration**: strict `git apply` with a dry run first — it applies fully or changes nothing.
  Commits only the patch's own paths, so anything you had staged stays yours.
- **Batches**: dependency waves, disjoint-scope validation, and a `finish` that integrates in
  order, runs the full suite once, and asserts no worktrees or `monkey/*` branches are left.
- **Limits per task**: wall-clock, stall watchdog, per-command timeout, recursion, USD budget and
  a token budget (so the cap holds even when pricing is unknown).
- **Status line**: an orange badge with the active profile and live worker counts.
- **Configuration by conversation**: profiles and defaults in `~/.monkey-army/config.json`, the
  API key entered through a secure dialog into `credentials.json` (0600). No environment
  variables, no file editing, no restart.

### Security

- Workers cannot push, fetch, merge, rebase or reach your credentials. Enforced twice: a git
  command allowlist at the tool layer (covering `env git`, `xargs git`, `sh -lc`, `$(…)` and the
  `-c`/`-C`/`--git-dir`/`--work-tree` global options), and `protocol.allow=never` plus SSH-agent
  removal in the worker's environment.
- Secrets never enter the model conversation; the worker process receives only its own key.
- Honest gap: shell commands inside the worktree are not sandboxed. Treat a worktree as
  attacker-adjacent, not attacker-proof.

### Found and fixed by live validation

Everything above passed an offline end-to-end suite first. Running it against a real router and a
real reasoning model then surfaced defects no fake could have shown:

- **Workers ran their tests against the whole disk.** `virtual_mode` shows the worktree as `/`,
  so models wrote `cd / && pytest` — which in the shell collects from the real filesystem root.
  Workers may no longer `cd` to an absolute path, and the prompt says why.
- **Timeouts left processes running.** `uv run` moves its child into its own process group, so
  killing the group missed it; two dozen disk scans survived their workers for up to 1h47m.
  `kill_tree` now walks every descendant.
- **Healthy workers were killed as "stalled".** The watchdog read stdout silence as a hang, but
  one reasoning call is silent for minutes. The worker now heartbeats (naming the command or
  model call in flight), each model request has its own timeout, and `stall_s` is 300.
- **A slow router could hang dispatch.** urllib's timeout is per socket read, so a trickling
  server kept a 30 s probe alive for 282 s. Every endpoint request has a hard total deadline.
- **Probes misreported capable models.** A 16-token budget truncated the tool call; the probe now
  allows 64 tokens and accepts `finish_reason: tool_calls` as evidence. Router replies with
  trailing `data: [DONE]` or SSE frames are parsed instead of crashing the tool call.
- **The token cap could be overshot without limit.** It read litellm's callback, which lags on a
  background thread; it now counts the usage reported on the streamed messages themselves. A
  stop caused by either cap is reported as such, even when nothing had changed yet.
- **Paths through `/private` on macOS** mirrored into worktrees and tripped the scope check; the
  state directory is resolved to its real path.

### Known limitations

- Live validation: Phase 1 24/24, Phase 2 45/46 (one skip: a well-behaved model cannot be forced
  out of scope), Phase 4 13/13 — see `docs/VALIDATION.md`.
- Phase 3 of the validation plan (the A/B token measurement against an unassisted session) is
  yours to run; `docs/TOKEN-ECONOMICS.md` has the protocol and an empty results table.
- No container sandbox for workers (see `ROADMAP.md`).
- Tool-result compression on the worker's key corrupts what the worker reads — turn RTK/Caveman
  compression off for it.
