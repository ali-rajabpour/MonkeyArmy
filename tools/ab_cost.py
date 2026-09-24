#!/usr/bin/env python3
"""Per-turn cost attribution for a Claude Code session transcript.

Answers "which turns actually cost money", which is what the A/B runs in
docs/TOKEN-ECONOMICS.md needed. Absolute token prices are not published per model, so
per-turn dollars are the official price *ratios* (output 5x, cache-read 0.1x,
cache-write 1.25x input) normalised to the session total you read off /cost.

    python3 tools/ab_cost.py ~/.claude/projects/<slug>/<id>.jsonl 1.38

Transcripts repeat one assistant response across several lines, each carrying the same
usage block, so turns are deduplicated by requestId before anything is summed.
"""

import json
import sys
from collections import defaultdict

RATIO = {"out": 5.0, "cr": 0.1, "cw": 1.25, "inp": 1.0}
MCP_PREFIX = "mcp__plugin_monkey-army_monkeys__"


def load(path):
    seen, order = {}, []
    with open(path) as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "assistant":
                continue
            msg = d.get("message", {})
            usage = msg.get("usage") or {}
            key = d.get("requestId") or msg.get("id")
            if not usage or key is None:
                continue
            turn = seen.get(key)
            if turn is None:
                turn = {
                    "out": usage.get("output_tokens", 0),
                    "inp": usage.get("input_tokens", 0),
                    "cr": usage.get("cache_read_input_tokens", 0),
                    "cw": usage.get("cache_creation_input_tokens", 0),
                    "tools": [],
                }
                seen[key] = turn
                order.append(turn)
            turn["tools"] += [
                c.get("name")
                for c in msg.get("content", [])
                if isinstance(c, dict) and c.get("type") == "tool_use"
            ]
    return order


def label(turn):
    names = [t.replace(MCP_PREFIX, "mk.") for t in turn["tools"]]
    return ",".join(names) or "(text only)"


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    path, total = argv[1], float(argv[2])
    turns = load(path)
    if not turns:
        print(f"no assistant turns with usage in {path}", file=sys.stderr)
        return 1
    units = [sum(RATIO[k] * t[k] for k in RATIO) for t in turns]
    scale = total / sum(units)

    first, last = turns[0], turns[-1]
    print(
        f"{len(turns)} requests, ${total:.2f}, "
        f"context {first['cr'] + first['cw']:,} -> {last['cr'] + last['cw']:,} tokens"
    )
    print(f"{'#':>3} {'$':>6} {'out':>6} {'ctx-in':>9}  tools")
    for i, (turn, unit) in enumerate(zip(turns, units), 1):
        print(
            f"{i:>3} {unit * scale:>6.3f} {turn['out']:>6} "
            f"{turn['cr'] + turn['cw']:>9,}  {label(turn)}"
        )

    groups = defaultdict(lambda: [0, 0.0, 0])
    for turn, unit in zip(turns, units):
        g = groups[label(turn)]
        g[0] += 1
        g[1] += unit * scale
        g[2] += turn["out"]
    print("-- by tool --")
    for name, (n, cost, out) in sorted(groups.items(), key=lambda kv: -kv[1][1]):
        print(f"  ${cost:>5.3f}  n={n:<3} out={out:<6} {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
