---
description: Fix the monkeys' configuration — change a profile's model, URL, prices or fallback, re-enter the API key, remove a profile, or switch the default.
argument-hint: "[what is wrong, e.g. 'wrong model' or 'remove profile coder']"
---

Repair an existing configuration. Use the smallest action that fixes what the user names; never
reset unless they ask to start over (that is `/monkey-reset`).

1. `configure(action="status")` first — know what exists before changing it.
2. Then, matching `$ARGUMENTS` (ask which, if it is ambiguous):
   - wrong model / URL / prices / fallback → `configure(action="set_profile", name=<same name>, ...)`
     with corrected values. The same name overwrites in place; the stored key is untouched.
   - wrong or rotated key → `configure(action="store_key", profile=<profile>)` **without** a `key`
     argument so the secure dialog opens. Only take a key typed in chat if the dialog is
     unavailable, and then tell the user once to rotate it later.
   - unwanted profile → `configure(action="remove_profile", name=<profile>)`.
   - wrong default → `configure(action="set_default", name=<profile>)`.
   - not sure which combo → `configure(action="discover_models", profile=<profile>)` and let them pick.
3. `configure(action="probe", profile=<profile>)` after any change that touches the model, URL or
   key, and report the result.
4. Finish with `configure(action="status")` so the user sees the new state.
