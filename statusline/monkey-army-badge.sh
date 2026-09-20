#!/usr/bin/env bash
# monkey-army — status-line BADGE.
#
# Prints one short orange segment when the monkeys are configured, so a
# combined status line can show it next to other plugins' badges:
#
#   🐒 deepseek-combo              configured, nothing running
#   🐒 deepseek-combo ⏳2 ⚠1 $0.12  two workers running, one asking, spend so far
#
# Prints NOTHING (exit 0) when no profile is configured — an unconfigured
# plugin should not take up room in the status line.
#
# Use it either way:
#   - as a segment, called from your own status-line script (see README), or
#   - directly:  "statusLine": { "type": "command",
#                                "command": "~/.claude/monkey-army-badge.sh",
#                                "refreshInterval": 2 }
#
# No jq, no python: the status-line shell is minimal on some platforms, and a
# badge that needs a dependency is a badge that silently disappears.

cat >/dev/null 2>&1  # drain the session JSON on stdin; this badge doesn't need it

home="${MONKEY_ARMY_HOME:-$HOME/.monkey-army}"
config="$home/config.json"
[ -f "$config" ] || exit 0

# default_profile from config.json without a JSON parser. Quoted string value,
# or `null` when the store has profiles but no recorded default.
profile=$(sed -n 's/.*"default_profile"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$config" | head -1)
[ -n "$profile" ] || exit 0

ORANGE=$'\033[38;5;208m'
DIM=$'\033[2m'
RESET=$'\033[0m'

badge="${ORANGE}🐒 ${profile}${RESET}"

# Live worker activity, if the server wrote a line recently. Same file the
# full-line reader uses: line 1 = expiry epoch, line 2 = rendered text.
f="$home/statusline"
if [ -f "$f" ]; then
  until_epoch=$(head -1 "$f" 2>/dev/null)
  case "$until_epoch" in
    ''|*[!0-9]*) ;;  # missing/non-numeric: no activity to show
    *)
      now=$(date +%s)
      if [ "$now" -le "$until_epoch" ]; then
        line=$(tail -n +2 "$f" 2>/dev/null)
        # Strip the pre-rendered line's own colors so the badge stays one color.
        line=$(printf '%s' "$line" | sed $'s/\033\\[[0-9;]*m//g')
        [ -n "$line" ] && badge="${badge} ${DIM}${line}${RESET}"
      fi
      ;;
  esac
fi

printf '%s' "$badge"
