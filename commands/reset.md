---
description: Delete every monkeys profile and stored API key and start configuration from scratch. Two-step; keeps task history, patches and notes.
---

Wipe the monkeys' provider configuration. This is the big hammer — if the user only got one
field wrong, stop and point them at `/monkey-army:repair` instead.

1. `configure(action="reset")` with no confirmation. It performs nothing: it reports which
   profiles and files *would* be deleted.
2. Show the user that list verbatim and ask them to confirm. State plainly that
   `~/.monkey-army/repos/` — jobs, patches, notes, worktrees — is kept, and that the stored API
   key will be deleted and has to be entered again.
3. Only on an explicit yes: `configure(action="reset", text="confirm")`.
4. Report what was deleted, then offer to run `/monkey-army:setup` to configure from scratch.

If the user asked to reset because something is broken rather than because they want a clean
slate, run `configure(action="doctor", repo_path=<repo>)` first — a reset does not fix a
missing `uv`, an unreachable 9Router, or a combo without tool calling.
