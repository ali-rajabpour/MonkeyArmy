"""Read a micro-spec out of a file instead of the supervisor retyping it.

Measured on the run-2 A/B benchmark: 12.8k of the supervisor's 18.2k output tokens were
micro-spec prose, and the brief it was transcribing already existed on disk, sectioned
one-to-one with the tasks. dispatch_task(spec_file=..., spec_section=...) lets the server do
that reading, which also keeps the brief out of the supervisor's conversation.
"""

from __future__ import annotations

import re
from pathlib import Path

MAX_SPEC_BYTES = 64_000

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def _norm(s: str) -> str:
    """Compare headings on words only, so '## 3. calc/convert.py — units' matches
    'calc/convert.py'."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def read_spec(spec_file: str, spec_section: str | None = None) -> str:
    """Return spec_file's text, or just the named Markdown section of it.

    Raises ValueError with a message meant for the supervisor — a wrong path or a
    heading that doesn't exist must fail the dispatch loudly, never dispatch a worker
    with an empty or half-read spec.
    """
    path = Path(spec_file).expanduser()
    if not path.is_file():
        raise ValueError(f"spec_file not found: {spec_file}")
    size = path.stat().st_size
    if size > MAX_SPEC_BYTES:
        raise ValueError(f"spec_file is {size} bytes (limit {MAX_SPEC_BYTES}); pass spec_section")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"cannot read spec_file: {type(e).__name__}: {e}") from e

    if not spec_section:
        if not text.strip():
            raise ValueError(f"spec_file is empty: {spec_file}")
        return text

    lines = text.splitlines()
    want = _norm(spec_section)
    start = level = None
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if not m:
            continue
        if start is None:
            title = _norm(m.group(2))
            if title == want or want in title:
                start, level = i, len(m.group(1))
            continue
        if len(m.group(1)) <= level:  # next sibling or parent heading ends the section
            return "\n".join(lines[start:i]).strip()
    if start is None:
        headings = [m.group(2) for m in (_HEADING.match(x) for x in lines) if m]
        raise ValueError(
            f"spec_section {spec_section!r} not found in {spec_file}; headings are: {headings}"
        )
    body = "\n".join(lines[start:]).strip()
    if not body:
        raise ValueError(f"spec_section {spec_section!r} is empty in {spec_file}")
    return body
