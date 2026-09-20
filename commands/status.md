---
description: Show the monkeys' configuration and health — profiles, default, stored key presence, last probe and doctor run. Read-only.
---

Report the monkeys' current state. Change nothing.

1. `configure(action="status")` — profiles, default profile, whether each profile's key is
   available, the defaults block, last probe results, `last_doctor_at`.
2. If the user asks "is it working?" or nothing has been probed recently, also run
   `configure(action="probe", profile=<default>)` and report `ok`, `latency_ms`, `tool_calling`,
   `usage_present`.
3. If `$ARGUMENTS` names a repo path, add `configure(action="doctor", repo_path=<path>)`.

Present it as a short table: profile, model, api_base, key set, default?, last probe. Never
print a key or any part of one. If no profile exists, say so and point at `/monkey-army:setup`.
