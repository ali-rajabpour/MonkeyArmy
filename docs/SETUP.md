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

## 4. Configure by talking (no restart, no env vars)
Run `/monkey-setup` (saying "monkeys: set up" also triggers it). The supervisor will:
1. run `doctor` (checks uv, git, worker dependencies, 9Router reachability),
2. ask for your 9Router URL,
3. ask for the API key through a secure dialog (it never appears in the chat); if your
   client has no dialog, you can type the key in the chat instead and the supervisor stores
   it for you — it will tell you to rotate that key afterwards, since it passed through the
   conversation and the session transcript,
4. list your combos and ask which one to use (and optional fallback),
5. save it as the default profile and probe it (one tiny request, confirms tool-calling works).

Manual equivalent, if you prefer explicit commands in chat:
- "configure: set profile `deepseek-combo`, model `openai/combo/<id>`, api_base
  `http://100.64.0.1/v1`, key var `MONKEY_9ROUTER_KEY`, prices 0.27 / 1.10 per Mtok"
- "configure: store key for `deepseek-combo`"  → dialog (or paste the key in chat and it is
  stored directly, with a rotate-it reminder)
- "configure: probe `deepseek-combo`"

Config lives in `~/.monkey-army/config.json`; the key in `~/.monkey-army/credentials.json` (0600).

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

## Tuning (all via "configure: …", only when you ask)
- Budget per task: `limits.max_budget_usd` (default $0.50) and `limits.max_tokens_total`.
- Diff cap: `defaults.max_diff_lines` (300) — larger diffs fail on purpose; split the task.
- Integration: `defaults.integrate_mode` = `commit` (squash commit per task) or `stage`.
- Long tool waits: `wait_for_tasks` blocks up to 120 s by default; if your Claude Code MCP tool
  timeout is lower, raise it via the `MCP_TOOL_TIMEOUT` environment variable — check your Claude
  Code version's docs for the exact name and default, since these have changed across releases.

## Removing the old skill
If you previously used a personal `CTOwithMonkeyArmy` skill, delete it — its content is now the
plugin's `monkey-army` skill.
