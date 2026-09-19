---
name: monkey-assess
description: When the user asks for implementation work that spans several files or is mostly mechanical typing, quickly judge whether it should be delegated to cheap workers with /monkey-army instead of being typed by this expensive model. Advice only; never dispatches.
---

# Monkey assess (advice only)

Apply the rules from the monkey-army skill's Assessment mode:
- < ~20 lines or unknown-cause debugging → DO IT YOURSELF.
- Multi-file mechanical or fully understood work whose code will be ≥ 3× the spec → DELEGATE.
- Otherwise BORDERLINE (name the deciding factor).

Output one line: `DELEGATE | DO IT YOURSELF | BORDERLINE — <reason>`, and if DELEGATE add
"Run `/monkey-army` to proceed." Do not decompose, dispatch, or call any `monkeys` tool.
