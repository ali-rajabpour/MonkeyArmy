---
name: assess
description: When the user asks for implementation work that spans several files or is mostly mechanical typing, quickly judge whether it should be delegated to cheap workers with /monkey-army instead of being typed by this expensive model. Advice only; never dispatches.
---

# Monkey assess (advice only)

Apply the rules from the monkey-army skill's Assessment mode:
- < ~300 lines of code in total, a request that is already a complete spec, or unknown-cause
  debugging → DO IT YOURSELF. (Measured: a fully-specified ~115-line feature cost 2.8× more
  delegated than written directly — the supervisor's fixed overhead outweighed it.)
- Multi-file mechanical or fully understood work whose code will be ≥ 3× the spec → DELEGATE.
- Otherwise BORDERLINE (name the deciding factor).

Output one line: `DELEGATE | DO IT YOURSELF | BORDERLINE — <reason>`, and if DELEGATE add
"Run `/monkey-army` to proceed." Do not decompose, dispatch, or call any `monkeys` tool.
