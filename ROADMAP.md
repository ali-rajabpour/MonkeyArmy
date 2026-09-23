## Before release

- [x] Live 9Router validation: `docs/VALIDATION.md` Phases 0-2 and 4 against a real combo
      (2026-09-21: Phase 1 24/24, Phase 2 45/46, Phase 4 13/13).
- [ ] Phase 3 A/B measurement (user-run) and the results table in `docs/TOKEN-ECONOMICS.md`.
- [x] `chore(release): 0.1.0` and the `v0.1.0` tag.
- [x] Push to `github.com/ali-rajabpour/MonkeyArmy` so the plugin is installable from a URL
      (`claude plugin marketplace add ali-rajabpour/MonkeyArmy`). Keep the local directory
      marketplace for development — it loads in place, so edits are live without a push.
- [ ] Apply to Anthropic's official plugin marketplace once the above is done and the plugin has
      been used on real work for a while.

## Open follow-ups

- [ ] **M8ven listing is stale — check it re-reads.** As of 2026-09-23 the listing still shows
      commit `55208e7` (Sep 22) and reports "13/13 tools missing one or more hints", a finding
      fixed in `5f6a095` / v0.2.1. They advertise a re-check on every push via their GitHub App.
      Verify with:
      ```bash
      curl -sL https://m8ven.ai/mcp/ali-rajabpour/monkeyarmy | grep -o 'commit: [0-9a-f]\{7\}'
      git -C . rev-parse --short HEAD
      ```
      If the commit still lags after a few days: use "Confirm or correct these findings" /
      "Dispute a finding" on the listing page, then email them — the annotation finding is the
      only scored item that was ever actionable, and it is already fixed. The C grade itself is
      an adoption cap, not something a re-read changes.
- [x] Phase 3 A/B on *large* mechanical work — run 2 (2026-09-24, ~730 lines over 10 files,
      5 tasks, all first attempt) measured **1.8× worse** delegated: $0.82 alone against $1.38
      supervisor + ~$0.10 workers. Better than run 1's 2.8×, still a loss. Quality was equal —
      each run's suite passes against the other run's implementation. Details and the cost
      breakdown in `docs/TOKEN-ECONOMICS.md`.
- [ ] **Cut the supervisor's fixed overhead**, which is what the A/B runs measured. In order of
      expected return: spec-by-reference in `dispatch_task` (a file path plus a section, so the
      supervisor stops retyping the brief as output tokens); a batched review call; a
      server-computed risk report so full-diff review is reserved for flagged tasks. The last one
      trades part of invariant I5 for most of its cost and needs an explicit decision first.

# Roadmap

## Later

- Container sandbox for workers.
- Per-task cost reconciliation against 9Router's usage log.
- Batch-level parallel integration.
- Optional LLM pre-review by a cheap model as a *hint* (never a gate).
