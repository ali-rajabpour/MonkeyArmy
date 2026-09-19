# monkey-army status line — token-free (PowerShell variant).
#
# Use this only when Git Bash is NOT installed, so Claude Code routes the
# status line through PowerShell. Otherwise prefer the .sh reader.
#
# All rendering happens in the MCP server (Python), which writes a pre-baked
# line to ~/.monkey-army/statusline (line 1 = expiry epoch, line 2 = text).
# This reader prints line 2 while the file is still fresh.
#
# Wire it in ~/.claude/settings.json:
#   "statusLine": { "type": "command",
#     "command": "powershell -NoProfile -File C:/Users/<you>/.claude/monkey-army-statusline.ps1",
#     "refreshInterval": 2 }

$null = $input | Out-String  # drain stdin JSON; unused

$f = Join-Path $HOME ".monkey-army/statusline"
if (-not (Test-Path $f)) { exit 0 }

$lines = Get-Content -LiteralPath $f -ErrorAction SilentlyContinue
if ($lines.Count -lt 2) { exit 0 }

$until = 0
if (-not [int64]::TryParse($lines[0], [ref]$until)) { exit 0 }

$now = [int64][double]::Parse((Get-Date -UFormat %s))
if ($now -le $until) {
    # PowerShell doesn't emit raw ANSI reliably on all hosts; strip escapes.
    ($lines[1..($lines.Count - 1)] -join "`n") -replace "`e\[[0-9;]*m", ""
}
exit 0
