# Review checklist (you, not a subagent)

Gate (must all be true before reading):
- [ ] `verification.passed == true`  - [ ] `scope.ok == true`  - [ ] status `succeeded`

Correctness
- [ ] Does exactly what the spec says — nothing more, nothing less
- [ ] Edge cases named in the spec are handled; no silent fallbacks, no swallowed exceptions
- [ ] Types/signatures match the spec verbatim
- [ ] Tests are meaningful (would fail if the change were wrong), not tautological

Fit
- [ ] Matches the file's existing style, naming, error-handling and import conventions
- [ ] No new dependency, no unrelated refactor, no reformatting noise, no leftover debug output
- [ ] Comments only where the existing code would have them

Safety
- [ ] No secrets, no network calls, no file writes outside the task's purpose
- [ ] No change to build/CI/security configuration unless the spec asked

Decision
- approve → `review_task(id, "approve")`, then `integrate_task(id)`
- reject → `review_task(id, "reject", "<exact problem> — root cause: <…> — fix: <…>")`
