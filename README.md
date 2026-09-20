# Monkey Army

Opus commands, monkeys type, Opus signs off. A Claude Code plugin that delegates
micro-tasks from a frontier-model supervisor to cheap workers (DeepSeek & co. via
your local 9Router), isolated in disposable git worktrees, verified by the server,
and reviewed and merged back by the supervisor — never idle tokens spent on typing.

## What happens

1. **Assess** — the supervisor decides whether a task is even worth delegating.
2. **Decompose** — the task is broken into micro-specs, each touching ~1 file.
3. **Dispatch** — each spec goes to a cheap worker in its own git worktree.
4. **Verify** — the server re-runs tests/lint/build and checks scope and diff size.
5. **Review** — the supervisor reads every diff and approves or rejects it.
6. **Merge back** — approved work lands on your branch as a squash commit; the
   worktree and branch are cleaned up. No leftovers, ever.

## Install in 5 steps

1. Install `uv` (the only runtime the workers need).
2. Have 9Router running with a combo (DeepSeek + fallbacks) and an API key.
3. `/plugin marketplace add ali-rajabpour/MonkeyArmy` then
   `/plugin install monkey-army@monkey-army`.
4. Export `MONKEY_9ROUTER_BASE_URL`, `MONKEY_9ROUTER_KEY`, `MONKEY_WORKER_MODEL` (see
   [`.env.example`](.env.example)), restart Claude Code, then run `/monkey-setup` to verify.
5. Try it on a scratch repo: `/monkey-army add a subtract(a, b) function to calc/__init__.py...`.

Full walkthrough: [`docs/SETUP.md`](docs/SETUP.md).

## What a run looks like (illustrative)

```
> /monkey-army add input validation to the signup form, with tests

Assessing... DELEGATE (mechanical, well-scoped).
Decomposed into 2 tasks: validate-email, validate-password.
Dispatched both. Waiting...
validate-email: succeeded, verification passed, scope ok, 34 lines.
Reviewed diff — approved. Integrated as one commit.
validate-password: succeeded, verification passed, scope ok, 41 lines.
Reviewed diff — approved. Integrated as one commit.
Batch finished: 1 worktree, 0 monkey/* branches left, full suite green.
Worker cost: $0.11 / 6,200 tokens. Compare with your /cost.
```

## Safety in one paragraph

Workers never touch your working tree: every write happens in a disposable git
worktree outside your repository, and a task only merges after the server
re-runs the acceptance command itself and the supervisor gives an explicit
approval — there is no path around either gate. Git is neutralised for workers
at the tool layer (an allowlist blocks push/fetch/merge/rebase/checkout and
similar, plus `git commit`/`git reset`, and `-c`/`-C`/`--git-dir`/`--work-tree`/
`--exec-path`/`--namespace`) and by environment (`protocol.allow=never`, no
credential helper, SSH agent removed), and secrets are filtered out of worker
shell commands. Integration only ever applies the patch's own paths, dry-runs
before writing anything, and refuses on an empty diff. Be aware that shell
commands the worker runs inside its worktree are not sandboxed beyond that —
this is process isolation and gate enforcement, not a container.

## Docs

- [`docs/SETUP.md`](docs/SETUP.md) — install and configure
- [`docs/DESIGN.md`](docs/DESIGN.md) — architecture, invariants, security model
- [`docs/TOKEN-ECONOMICS.md`](docs/TOKEN-ECONOMICS.md) — why delegation saves tokens, and when it doesn't
- [`docs/VALIDATION.md`](docs/VALIDATION.md) — the validation checklist this plugin was tested against
- [`ROADMAP.md`](ROADMAP.md) — what's deliberately not built yet

## Lineage

Derived from cc-delegate by Etienne Lescot, MIT licensed.

## License

MIT — see [`LICENSE`](LICENSE).
