## Before release

- [x] Live 9Router validation: `docs/VALIDATION.md` Phases 0-2 and 4 against a real combo
      (2026-09-21: Phase 1 24/24, Phase 2 45/46, Phase 4 13/13).
- [ ] Phase 3 A/B measurement (user-run) and the results table in `docs/TOKEN-ECONOMICS.md`.
- [x] `chore(release): 0.1.0` and the `v0.1.0` tag.
- [ ] Push to `github.com/ali-rajabpour/MonkeyArmy` so the plugin is installable from a URL
      (`claude plugin marketplace add ali-rajabpour/MonkeyArmy`). Keep the local directory
      marketplace for development — it loads in place, so edits are live without a push.
- [ ] Apply to Anthropic's official plugin marketplace once the above is done and the plugin has
      been used on real work for a while.

# Roadmap

## Later

- Container sandbox for workers.
- Per-task cost reconciliation against 9Router's usage log.
- Batch-level parallel integration.
- Optional LLM pre-review by a cheap model as a *hint* (never a gate).
