---
name: monkey-setup
description: Guided one-time setup of the monkeys MCP server — 9Router URL, profile, API key (via secure dialog, never typed in chat), combo selection, probe. Triggered explicitly by "monkeys: set up", "set up monkey army", "configure the monkeys", or /monkey-setup. Do not use for a single config tweak the user names directly (use configure(...) yourself) or for running a delegation batch (use /monkey-army).
---

# Monkey Setup — guided first-run configuration

The user asked you to set the monkeys up. Every mutating `configure` call in this flow counts
as user-requested because the user asked for setup by name — you do not need to re-confirm each
one. Anything outside this flow (changing a profile later, switching the default, etc.) still
needs an explicit request in that conversation; do not reuse this permission for it.

Run the steps in order. Do not skip ahead or batch calls that depend on the user's answer.

## 1. Check current state
`configure(action="status")` — see what profiles/default already exist.
`configure(action="doctor", repo_path=<repo, if known>)` — checks uv, git, worker deps, 9Router
reachability. Fix anything it flags (report it, don't silently continue) before proceeding.

## 2. 9Router URL
Ask the user for their 9Router URL. Default if they have none ready: `http://localhost:20128/v1`.

## 3. Profile name
Ask what to call the profile. Default: `deepseek-combo`.

## 4. Create the profile (placeholder model)
```
configure(action="set_profile", name=<profile>, model="openai/combo/<placeholder>",
          api_base=<url>, api_key_env_var="MONKEY_9ROUTER_KEY")
```
This is provisional — the model id is filled in for real at step 6.

## 5. Store the key
```
configure(action="store_key", profile=<profile>)
```
Call it **without** a `key` argument first — that opens a secure dialog and the key never enters
the chat. This is the path to prefer, always.

If the dialog is unavailable (the client does not support elicitation, and the tool says so) or
the user asks to enter the key directly, take it in the conversation and store it:
```
configure(action="store_key", profile=<profile>, key="<key the user gave>")
```
Then say plainly, once: the key passed through the model conversation and is written to this
session's transcript on disk, so rotate it in the 9Router dashboard when convenient and re-enter
the new one through the dialog. Do not repeat the key back, do not echo it in a summary, and do
not put it in a note. Never ask the user to paste a key when the dialog is available — offer the
dialog first and let them choose.

The key is saved to `~/.monkey-army/credentials.json` (mode 0600) either way. The user never
edits a file and never sets an environment variable.

## 6. Discover combos
```
configure(action="discover_models", profile=<profile>)
```
Present the returned combos and ask the user which one to use. Optionally also ask for
input/output prices per Mtok and a fallback model.

## 7. Finalize the profile
```
configure(action="set_profile", name=<profile>, model="openai/combo/<chosen>", api_base=<url>,
          api_key_env_var="MONKEY_9ROUTER_KEY", fallback_models=[...]?,
          price_input_per_mtok=<?>, price_output_per_mtok=<?>)
```

## 8. Set as default
`configure(action="set_default", name=<profile>)`

## 9. Probe
`configure(action="probe", profile=<profile>)`

Report the result: `ok`, `latency_ms`, `tool_calling`, `usage_present`. If `tool_calling` is
`"not_observed"`, warn the user plainly — deepagents needs tool calling to work at all — and
suggest they pick a different combo and repeat from step 6.

Remind the user once: in 9Router, turn **off** RTK/Caveman tool-result compression for this key.
Compressed tool results corrupt what the worker sees.

Tell the user setup is done and they can run `/monkey-army` next.
