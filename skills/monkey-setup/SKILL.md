---
name: monkey-setup
description: Verify the monkeys MCP server's environment configuration and report what's missing — read-only checks (status, doctor, probe, discover_models), never mutates anything. Triggered explicitly by "monkeys: set up", "set up monkey army", "configure the monkeys", or /monkey-setup. Do not use for running a delegation batch (use /monkey-army).
---

# Monkey Setup — verification and guidance

Configuration is environment variables only — there is no config file, no profile, and nothing
in this flow writes anything. Every step below is read-only. If a variable is missing or
invalid, tell the user the exact `export` line to add and that Claude Code needs a restart to
pick it up; do not try to work around it.

## 1. Check status
`configure(action="status")` — reports every configuration variable: set/missing/invalid, and
its resolved value or default. **Never echo the value of `MONKEY_9ROUTER_KEY`** — report only
whether it is set.

If anything required is missing or invalid, stop here and print the exact lines the user should
add, e.g.:
```
export MONKEY_9ROUTER_BASE_URL=http://100.64.0.1/v1
export MONKEY_9ROUTER_KEY=<your key>
export MONKEY_WORKER_MODEL=openai/combo/<id>
```
Tell them to put these in their shell profile (or `~/.zshenv`) and **restart Claude Code**, then
re-run `/monkey-setup`. Do not proceed to the next step until status is clean.

## 2. Doctor
`configure(action="doctor", repo_path=<repo, if known>)` — checks uv, git, worker dependencies,
9Router reachability. Report anything it flags; don't silently continue.

## 3. Discover models
`configure(action="discover_models")` — lists the model combos the configured key can see, so
the user can confirm `MONKEY_WORKER_MODEL` is the right one or pick a better one. If they want to
change it, tell them to export the new value and restart Claude Code — this skill cannot set it
for them.

## 4. Probe
`configure(action="probe")` — one tiny request against the configured model.

Report the result: `ok`, `latency_ms`, `tool_calling`, `usage_present`. If `tool_calling` is
`"not_observed"`, warn the user plainly — deepagents needs tool calling to work at all — and
suggest a different combo (repeat from step 3 after they change `MONKEY_WORKER_MODEL` and
restart).

Remind the user once: in 9Router, turn **off** RTK/Caveman tool-result compression for this key.
Compressed tool results corrupt what the worker sees.

Tell the user setup is verified and they can run `/monkey-army` next.
