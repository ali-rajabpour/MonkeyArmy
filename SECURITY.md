# Security policy

## Reporting a vulnerability

Email **ali@rajabpour.com** — please don't open a public issue for a security problem.

Include what you need to make it reproducible: the version or commit, what you ran, what
happened, and what you expected. You'll get an acknowledgement within a few days. If the report
is valid, you'll get a fix or a clear statement of why it won't be fixed, and credit in the
release notes unless you'd rather not be named.

This is a personal project with no paid support, so please don't expect a same-day response or a
bounty.

## Supported versions

The latest release only. Fixes land in a new version rather than in patches to old tags.

## What this plugin does with your machine

Monkey Army runs an MCP server locally and spawns worker subprocesses. What matters for
security review:

- **Workers write only inside a disposable git worktree** under `~/.monkey-army/`, outside your
  repository. Nothing they write reaches your working tree until you approve it and the server
  applies the patch.
- **Nothing merges without two gates:** the server re-runs the acceptance command itself, and the
  supervisor must record an explicit approval. Neither can be bypassed by a worker's claim.
- **Workers cannot reach the network through git or use your credentials.** A git command
  allowlist at the tool layer, plus `protocol.allow=never`, an emptied credential helper and
  removal of the SSH agent from the worker's environment.
- **Your API key is passed only to the worker process**, never into the model conversation.
  Everything that looks like a secret is filtered out of the environment workers hand to shell
  commands.
- **Known limitation, by design:** shell commands a worker runs inside its worktree are **not
  sandboxed**. There is no container and no seccomp profile. A worker that finds a way around the
  git allowlist is limited only by the controls listed above. Treat a worktree as
  attacker-adjacent, not attacker-proof, and don't point the plugin at a repository whose
  contents you would not run locally.

`docs/DESIGN.md` has the full security model, including which controls are enforced by the tool
layer, which by the environment, and which are advisory.

## Scope

In scope: anything that lets a worker escape its worktree, reach your credentials, alter shared
git refs, or get code merged without the server's verification and the supervisor's approval.

Out of scope: the behaviour of the models you point it at, your 9Router deployment, and the
un-sandboxed shell noted above — that one is documented rather than fixed.
