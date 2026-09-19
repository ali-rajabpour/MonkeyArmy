# Design

Stub — expanded in a later work package (architecture, invariants, data
model, lifecycle, security model, decisions).

## Known upstream caveats

### deepagents `LocalShellBackend` timeout can hang forever on Windows

**Status:** worked around in `worker/worker.py` (`SupervisedShellBackend`), not fixed upstream.

The stock backend runs commands with `subprocess.run(shell=True, timeout=...)`. On Windows,
CPython's `TimeoutExpired` path kills only the direct shell process and then calls
`communicate()` again **without a timeout** to collect output. If the killed shell had spawned
a grandchild (e.g. `bash → find`), the grandchild survives, keeps the inherited stdout pipe
open, and that second `communicate()` blocks forever — one stuck command freezes the whole
agent loop. Our backend runs the process itself and kills the entire process tree
(`taskkill /F /T` / POSIX process groups) on expiry.

### deepagents `virtual_mode` silently remaps absolute paths

**Status:** mitigated via the worker's system prompt (relative paths mandated), not fixed.

With `virtual_mode=True`, a file operation on an absolute path (`/c/Users/.../file.txt` or
`C:/...`) is interpreted as virtual-root-relative and lands at `<worktree>/c/Users/.../file.txt`
— silently misplaced, no error. Models produce absolute paths spontaneously (often echoing
`pwd` output), so greenfield files can end up in a junk subtree. Reproduced deterministically
with deepagents 0.7.0a6.
