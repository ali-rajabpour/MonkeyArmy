# Setup (5 steps, ~10 minutes)

Monkey Army runs inside Claude Code. Your Claude Code session keeps talking to Anthropic exactly
as before; only the *workers* use your 9Router.

## 1. Install uv (the only runtime)
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # macOS / Linux
# Windows (PowerShell): powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv --version
```
The first worker run downloads its Python dependencies once (1–3 minutes). `doctor` (step 4)
does this for you up front.

## 2. Have 9Router ready
- 9Router is running and reachable (default `http://localhost:20128/v1`; yours may be
  `http://100.64.0.1/v1`).
- You created a **combo** (Dashboard → Combos) with DeepSeek first and your other cheap models as
  fallback. Note its id; workers will use `combo/<id>`.
- Copy an API key from the 9Router dashboard.
- In 9Router, make sure **RTK / Caveman tool-result compression is OFF** for this key. Workers
  read files through tool results; compressing them corrupts what the worker sees.

## 3. Install the plugin in Claude Code
```
/plugin marketplace add ali-rajabpour/MonkeyArmy
/plugin install monkey-army@monkey-army
```
(Developing locally: `claude --plugin-dir /path/to/monkey-army`.) Run `/mcp` — you should see the
`monkeys` server with 13 tools.

## 4. Configure via environment, then verify
All configuration is environment variables — there is no config file and no in-chat setup dialog.
Export the required ones (see `.env.example` for the full list and every optional one):
```bash
export MONKEY_9ROUTER_BASE_URL=http://100.64.0.1/v1
export MONKEY_9ROUTER_KEY=<your key>
export MONKEY_WORKER_MODEL=openai/combo/<id>
```
Put these in `~/.zshenv` (or your shell's profile) to keep them across sessions. The MCP server
only sees what Claude Code inherited at launch, so **restart Claude Code** after exporting.

Then run `/monkey-setup` to verify. It calls `configure(action="status")` to confirm every
variable is set and valid, `configure(action="doctor")` to check uv/git/worker deps/9Router
reachability, `configure(action="discover_models")` to list combos if you need to pick
`MONKEY_WORKER_MODEL`, and `configure(action="probe")` to confirm tool-calling works end to end.
It never writes anything — if something is missing or invalid, it tells you the exact `export`
line to add and to restart Claude Code.

## 5. Try it on a scratch repository
```bash
cp -r <plugin>/examples/toy-repo /tmp/toy && cd /tmp/toy && git init -b main && git add . && git commit -m init
claude
```
`<plugin>` is wherever Claude Code installed this plugin (check with `/plugin`, or use the path
you passed to `--plugin-dir` if developing locally).

Then: `/monkey-army add a subtract(a, b) function to calc/__init__.py with tests; pytest must pass`.
Watch the plan, the review, and the single squash commit that lands on `main`.

## Optional: status line
```bash
cp <plugin>/statusline/monkey-army-statusline.sh ~/.claude/ && chmod +x ~/.claude/monkey-army-statusline.sh
```
`~/.claude/settings.json`:
```json
{ "statusLine": { "type": "command", "command": "~/.claude/monkey-army-statusline.sh", "refreshInterval": 2 } }
```

## Tuning (environment variables, see `.env.example`; changes need a Claude Code restart)
- Budget per task: `MONKEY_MAX_BUDGET_USD` (default $0.50) and `MONKEY_MAX_TOKENS_TOTAL`.
- Diff cap: `MONKEY_MAX_DIFF_LINES` (300) — larger diffs fail on purpose; split the task.
- Integration: `MONKEY_INTEGRATE_MODE` = `commit` (squash commit per task) or `stage`.
- Long tool waits: `wait_for_tasks` blocks up to `MONKEY_WAIT_TIMEOUT_S` (default 120s, hard cap
  170s). If your Claude Code MCP tool timeout is lower, raise it with the `MCP_TOOL_TIMEOUT`
  environment variable (milliseconds) or the per-server `timeout` field in your MCP server
  config — check your installed Claude Code version's docs for the current default and exact
  name, since these have changed across releases.

## Removing the old skill
If you previously used a personal `CTOwithMonkeyArmy` skill, delete it — its content is now the
plugin's `monkey-army` skill.
