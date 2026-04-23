# LiteClaw

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)](#platform-support)
[![CI](https://github.com/breaktheready/liteclaw/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/breaktheready/liteclaw/actions/workflows/ci.yml)
[![GitHub stars](https://img.shields.io/github/stars/breaktheready/liteclaw?style=social)](https://github.com/breaktheready/liteclaw/stargazers)

Control Claude Code CLI remotely via Telegram. No API key needed.

[한국어](README_KO.md)

---

## Why I made this

I'm not really a programmer — more of a power user who relies on Claude Code daily. When OpenClaw got cut off, I needed a way to keep using Claude Code from my phone.

My solution was pretty basic: a Python script that connects Telegram to a Claude Code session running in tmux. It types your message into the terminal (`send-keys`) and reads back what's on screen (`capture-pane`). That's it.

No API key needed. No extra cost. If you're already paying for Claude Code, this just lets you use it remotely.

It works for what I need, so I figured I'd share it in case others are looking for something similar.

---

## How is this different?

Unlike tools that call Claude's API directly (which means extra costs), LiteClaw operates your **existing Claude Code CLI session** through tmux. You're already paying for Claude Max — this just lets you use it from your phone.

- Single Python file (~900 lines), not a framework
- No API keys to Anthropic needed
- No containers, no Docker
- Your subscription covers everything

## Features

- **Remote access** — Control Claude Code from anywhere via Telegram
- **AI summarization** — Responses cleaned up by an LLM before delivery (toggleable)
- **Busy detection** — Knows when Claude is working, queues your message automatically
- **Progress updates** — Periodic status messages while Claude works on long tasks
- **File transfer** — Send files to the server and download results back to Telegram
- **Multi-target** — Switch between tmux sessions and windows on the fly
- **Photo upload** — Send photos for Claude's vision tasks
- **CLI Mirror** (`/mirror`) — Forward terminal direct-typed input to Telegram with debounce (off by default for security)
- **Draft Streaming** — Status messages edit in place, converging into a single final answer
- **Reasoning Lane** (`/reasoning`) — Claude's `(thinking)` blocks separated into a clean preface, final answer stays readable
- **Smart Interactive Prompts** — Yes/No prompts auto-generate Telegram inline keyboards; free-form answers (e.g. "the second one") auto-parse to option index
- **Skill System** — Extensible skills in Markdown or Python at `~/.liteclaw/skills/`. Register via `/lcskill`. Appear natively in Telegram command menu.
- **Multi-agent orchestration** — LiteClaw acts as org lead, spinning up independent peer agents in separate tmux sessions. New commands: `/agents`, `/agent new|status|remove`, `/assign`. Agent registry persists across restarts.
- **Auto-recovery** — Detects and recovers from API proxy downtime automatically. Re-authenticates Claude Code sessions on 401 errors and notifies you via Telegram when back online.
- **Unified notifications** — All Telegram messages route through a single `notify.py` module with summarizer cleanup. Falls back to raw output if the summarizer is unavailable.

---

## What's new in v0.6 (April 2026)

Focused on reliability, delivery quality, and agent continuity:

- **Global `liteclaw` CLI** — `liteclaw start|stop|restart|status|logs|attach` from any directory. `setup.sh` installs a symlink in `~/.local/bin/liteclaw`.
- **Pinned Claude Code session (`--session-id <uuid>`)** — `start.sh` allocates / adopts a stable UUID and always resumes the same conversation, even when other Claude Code windows are open in the same cwd.
- **Structured JSONL response path** — responses are now lifted from Claude Code's own session log (`~/.claude/projects/<cwd>/<id>.jsonl`) instead of scraping the tmux pane. No more ANSI chrome, no scroll-back truncation, no summarizer over-compression on long answers. Automatic fallback to the pane path if jsonl is unavailable.
- **OpenClaw-style memory layout** (`~/.liteclaw/`) — per-day transcripts, daily markdown digests (LLM-compacted), rolling strategic summary, and a startup primer assembled from recent + strategic context. Legacy `~/.liteclaw-history.jsonl` auto-migrates on first boot.
- **Boot-ready Telegram ping** — one-shot "🚀 LiteClaw ready" after init with resume state + primer size. Rate-limited to avoid dev-churn spam (`BOOT_NOTIFY`).
- **Cron hardening** — trust-prompt auto-accept, `is_idle_prompt` false-positive fix (Claude's UI chrome no longer jams the busy-wait), 300 s tolerance window with a stable-prompt fallback, and failures are captured with full pane snapshot to `~/.liteclaw/cron-error-capture.md` for later review.
- **`/recall session [uuid]`** — session-scoped conversation recall. Transcripts carry a compact integer alias (`sid`) into `sessions.json.history[]` so every row is ~70 % smaller than embedding the full UUID.
- **Quieter Telegram UX** — mid-poll status edits off by default (`SHOW_POLLING_STATUS=0`); completed jsonl turns skip the follow-up monitor so the final message is never overwritten with a stale status edit.

See `DEVNOTES.md` for the story behind these changes and the parking-lot for related ideas.

---

## Prerequisites

- **Python** 3.10+
- **tmux** 3.0+
- **Claude Code CLI** installed and authenticated (`claude --version` to verify)
- **Telegram bot token** from [@BotFather](https://t.me/BotFather)
- **(Recommended) OpenAI-compatible API proxy** for response summarization (Tier 1)
  - Without it, LiteClaw falls back to a hidden Claude Code agent (Tier 2) or raw output (Tier 3)
  - Recommended: [claude-max-api-proxy](https://github.com/mattschwen/claude-max-api-proxy) — reuses your Claude Max subscription, ships a `docker-compose.yml`
  - Any OpenAI-compatible endpoint works (OpenAI, Groq, local LLM, etc.) — just point `SUMMARIZER_URL` / `SUMMARIZER_MODEL` at it
  - The default `SUMMARIZER_URL` is `http://localhost:3456/v1` (matches the recommended proxy's default port)
- **(Optional) Docker** — only needed if your API proxy runs in a container

### Platform support

| Platform | Status | Notes |
|---|---|---|
| Linux | ✅ Primary target | Docker proxy works directly |
| macOS | ✅ Supported | Run the proxy as a **LaunchAgent** instead of Docker — Docker can't read the keychain. See [MAC-OPS.md](./MAC-OPS.md). Use [`start.sh`](./start.sh) to launch Claude Code + LiteClaw together. |
| Windows | ⚠️ Not native | LiteClaw depends on tmux. Run under **WSL** (Ubuntu/Debian) and follow the Linux path. Native PowerShell/cmd is not supported. |

---

## Quick Start

### Primary method: setup script

```bash
git clone https://github.com/breaktheready/liteclaw.git
cd liteclaw
bash setup.sh
```

The setup script creates the virtual environment, installs dependencies, and copies `.env.example` to `.env`.

### Manual installation

```bash
git clone https://github.com/breaktheready/liteclaw.git
cd liteclaw
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### Configure

Edit `.env` and set the required values:

```env
BOT_TOKEN=your_bot_token_here
CHAT_ID=your_numeric_chat_id_here
TMUX_TARGET=claude:1
```

### Start Claude Code in tmux

```bash
tmux new-session -s claude 'claude --dangerously-skip-permissions'
```

### Run LiteClaw

```bash
source .venv/bin/activate
python3 liteclaw.py
```

Send `/start` to your Telegram bot to confirm it is working.

### One-command launch (`start.sh` or global `liteclaw`)

`setup.sh` symlinks a global `liteclaw` dispatcher into `~/.local/bin/`, so once installed you can run from any directory:

```bash
liteclaw start            # bring up Claude Code (tmux 'claude') + LiteClaw daemon
liteclaw start --attach   # ... and attach to the claude pane after
liteclaw stop             # tear both down
liteclaw restart          # stop + start
liteclaw status           # tmux sessions, daemon pid, dashboard port
liteclaw logs [-f]        # tail /tmp/liteclaw_run.log
liteclaw attach           # tmux attach -t claude
```

The underlying `bash ./start.sh [--attach]` still works for direct invocation. It's idempotent — re-running is a no-op when both are already up. Order is enforced (Claude Code first, prompt-readiness wait, then LiteClaw) so the bridge always has a target. The "trust this folder" dialog is auto-accepted.

**Session pinning**: `start.sh` allocates (or adopts) a stable UUID on first run and stores it in `~/.liteclaw/sessions.json`, then launches `claude --session-id <uuid>`. Every subsequent `liteclaw start` resumes the *same* conversation — unaffected by other Claude Code windows you may open in the same cwd.

---

## Configuration

All settings are controlled via `.env`. Copy `.env.example` and edit as needed.

| Variable | Default | Description |
|----------|---------|-------------|
| `BOT_TOKEN` | (required) | Telegram bot token from @BotFather |
| `CHAT_ID` | (required) | Your numeric Telegram user ID |
| `TMUX_TARGET` | `claude:1` | Target tmux session and window (`SESSION:WINDOW.PANE`) |
| `SUMMARIZER_URL` | `http://localhost:3456/v1` | OpenAI-compatible API endpoint for summarization |
| `SUMMARIZER_MODEL` | `claude-sonnet-4-6` | Model to use for response cleanup |
| `SCROLLBACK_LINES` | `500` | Number of tmux history lines to capture per poll |
| `INTERMEDIATE_INTERVAL` | `10` | Seconds between progress updates while Claude works |
| `STAGING_DIR` | `~/liteclaw-files` | Directory where uploaded files are saved on the server |
| `EXTRA_PROMPT_PATTERNS` | (empty) | Comma-separated regex patterns for custom prompt detection |
| `PROXY_DIR` | `~/max_api_proxy` | API proxy directory for auto-recovery (`docker compose up -d`) |
| `DASHBOARD_PORT` | `7777` | Web dashboard port (set `0` to disable) |
| `HISTORY_FILE` | `~/.liteclaw-history.jsonl` | Legacy single-file conversation history (still written for back-compat with `/recall`) |
| `HISTORY_RECALL_LIMIT` | `50` | Max entries returned by `/recall` |
| `BOOT_NOTIFY` | `1` | Send a one-shot "LiteClaw ready" Telegram ping after init. `0` to disable. |
| `LITECLAW_DIR` | `~/.liteclaw` | Root of the OpenClaw-style memory layout (`transcripts/`, `memory/`, `sessions.json`, `primer.md`) |
| `CLAUDE_CWD` | `$HOME` | Pinned working directory where `start.sh` launches Claude Code. Used to locate the session JSONL under `~/.claude/projects/<encoded-cwd>/`. |
| `PRIMER_RECENT_TURNS` | `20` | Turns pulled from recent history when building `primer.md` |
| `USE_JSONL_RESPONSE` | `1` | Prefer Claude Code's session JSONL as the response source (falls back to pane scraping on failure). `0` to force pane path. |
| `SHOW_POLLING_STATUS` | `0` | Emit intermediate "Working…" status edits to Telegram while Claude works. Off by default — these were the source of garbled mid-answer updates. |

---

## Commands

| Command | Description |
|---------|-------------|
| Any text | Relay text directly to Claude Code and return the response |
| `/start` or `/help` | Show available commands and current configuration |
| `/status` | Display the last 30 lines of Claude's current tmux pane |
| `/target SESSION:WIN.PANE` | Switch to a different tmux target (e.g. `/target work:0`) |
| `/cancel` | Send Ctrl+C to interrupt Claude's current task |
| `/escape` | Send the Escape key (useful for exiting Claude modal dialogs) |
| `/raw` | Toggle between summarized and raw output |
| `/model MODEL_NAME` | Change the summarizer model (e.g. `/model claude-sonnet-4-6`) |
| `/sessions` | List all active tmux sessions |
| `/get FILEPATH` | Download a file from the server to Telegram |
| `/mirror on\|off\|status` | Enable/disable CLI Mirror (forward typed input to Telegram with debounce) |
| `/reasoning on\|off\|status` | Enable/disable Reasoning Lane (separate `(thinking)` blocks into preface) |
| Send a document | Upload a file to `STAGING_DIR` and relay its path (and contents for small files) to Claude |
| Send a photo | Upload a photo to `STAGING_DIR` and relay the path to Claude for vision tasks |

### Skill Commands

| Command | Description |
|---------|-------------|
| `/lcskill list` | Show all available skills |
| `/lcskill new NAME` | Create a new skill template (Markdown) |
| `/lcskill remove NAME` | Delete and unregister a skill |
| `/lcskill reload` | Reload all skills from disk |

### Multi-Agent Commands

| Command | Description |
|---------|-------------|
| `/agents` | List all registered agents |
| `/agent new NAME PATH` | Create a new Claude Code agent in a dedicated tmux session |
| `/agent status` | Detailed status of all agents with pane preview |
| `/agent remove NAME` | Kill agent tmux session and unregister |
| `/assign NAME TASK` | Send a task to a named agent and relay the response |

### Cron Commands

| Command | Description |
|---------|-------------|
| `/cron list` | Show all scheduled jobs |
| `/cron add ID CRON(5) PROJECT MSG` | Create a new cron job (5-field cron expression) |
| `/cron remove ID` | Delete and unschedule a job |
| `/cron enable/disable ID` | Toggle job execution |
| `/cron run ID` | Manual trigger for testing |
| `/cron log ID` | Show last run time and status |

### Command details

**Any text message** — LiteClaw checks if Claude is idle, injects your message into the tmux pane, polls for a response, optionally runs it through the summarizer, and sends it back. Long responses are split into 4000-character chunks automatically.

**`/status`** — Shows raw pane content without filtering. Useful for checking what Claude is doing mid-task or diagnosing prompt detection issues.

**`/target`** — Switches the active tmux target without restarting the bot. Accepts any valid tmux target format: `session`, `session:window`, or `session:window.pane`.

**`/cancel`** — Sends a SIGINT (Ctrl+C) to the active pane. Use this to abort a long-running Claude task before sending a new message.

**`/escape`** — Sends the Escape key sequence. Useful for closing Claude's permission dialogs or exiting selection mode.

**`/raw`** — Toggles raw mode. When enabled, responses are sent unfiltered (terminal noise and all). When disabled (default), the summarizer removes noise and formats the response for readability.

**`/model`** — Changes the summarizer model at runtime. Takes effect immediately for the next response.

**`/sessions`** — Runs `tmux list-sessions` and sends the output. Helps when you need to find the right session name for `/target`.

**`/get`** — Downloads a file from the server. Relative paths are resolved from the tmux pane's working directory. Absolute paths work as-is. Maximum 50 MB.

---

## Skills

LiteClaw supports extensible skills stored in `~/.liteclaw/skills/`. Skills can be written in **Markdown** (with YAML frontmatter) or **Python**.

### Markdown Skills

Markdown skills use a template syntax with YAML frontmatter:

```markdown
---
name: translate
description: Translate text to another language
---

Translate the following text to {{language}}:

{{text}}
```

**Placeholders** (`{{varname}}`) are replaced by arguments passed via Telegram. For example:

```
/translate language=Korean text=Hello world
```

This injects the prompt into Claude's session with values substituted.

### Python Skills

Python skills are executable modules with a standard interface:

```python
# my_skill.py
COMMAND = "my_skill"
DESCRIPTION = "Does something useful"

async def handler(args: dict, context: LiteClawContext) -> str:
    """Handler function. args contains parsed arguments."""
    result = await context.send_to_claude(f"Do work with {args.get('param')}")
    return result
```

Skill functions are executed in-context with access to LiteClaw's state.

### Skill Management

**Create a new skill** (Markdown template):

```
/lcskill new my_skill
```

This creates `~/.liteclaw/skills/my_skill.md` with a template. Edit and save — it's automatically reloaded within ~10s.

**List available skills**:

```
/lcskill list
```

Shows all registered skills and maps them to Telegram `/skillname` commands.

**Reload skills**:

```
/lcskill reload
```

Manually refresh all skills from disk (automatic hot-reload also runs every 10s).

**Remove a skill**:

```
/lcskill remove skill_name
```

Deletes the skill and unregisters from Telegram command menu.

### Telegram Command Menu

All registered skills appear natively in Telegram's command menu (the `/` autocomplete). LiteClaw periodically re-registers the menu to prevent pollution from other Telegram bridges (e.g. OpenClaw).

### Storage & Persistence

Skills are stored in `~/.liteclaw/skills/`:
- **Markdown**: `skill_name.md` with frontmatter
- **Python**: `skill_name.py` with COMMAND/DESCRIPTION/handler

Automatic migration from legacy `~/.liteclaw-evolve/skills/` on startup.

---

## File Transfer

### Upload (document or photo)

Send any file as a Telegram document attachment (up to 50 MB), or send a photo directly from your camera or gallery.

LiteClaw will:
1. Save the file to `STAGING_DIR` on the server
2. If the file is a small text file (under ~50 KB), embed its contents in the message to Claude
3. Otherwise, relay the file path so Claude can read it directly
4. Send Claude's response back to Telegram

Add a caption to your file to pass instructions alongside it. For example:

```
Caption: "Summarize the key findings in this report"
```

For photos, the caption is relayed as the prompt for Claude's vision capabilities.

### Download with `/get`

```
/get results.txt
/get ~/projects/output.json
/get /tmp/analysis_20260405.csv
```

The file is fetched and sent back as a Telegram document. Relative paths resolve from the tmux pane's current working directory.

---

## How It Works

```
You (Telegram) --> LiteClaw --> tmux send-keys --> Claude Code CLI
                                                        |
You (Telegram) <-- Summarizer <-- capture-pane <-- Response
```

1. Your message arrives at the Telegram bot
2. LiteClaw checks if Claude is idle (prompt visible) or busy
3. If idle, the message is injected into the tmux pane via `send-keys`
4. LiteClaw polls `capture-pane` every 1.5 seconds
5. When the pane content stabilizes and a prompt reappears, the response is ready
6. The response is optionally passed through the summarizer to remove terminal noise
7. The clean response is split into chunks and sent back to Telegram

### No API Key Required

LiteClaw does not call Claude's API directly. It controls Claude Code through tmux using your existing Claude Code subscription. Summarization is handled by a local OpenAI-compatible proxy (optional). If the summarizer is unavailable, LiteClaw falls back to raw output automatically.

---

## Summarizer Setup

LiteClaw has a 3-tier summarizer that works out of the box — no extra setup needed.

**Tier 1: API Proxy** (fastest, 2-3s) — If you have an OpenAI-compatible API endpoint, set `SUMMARIZER_URL`. Options:
- [claude-max-api-proxy](https://github.com/mattschwen/claude-max-api-proxy) — Use your Claude Max subscription as an OpenAI-compatible API (recommended Tier 1 summarizer)
- [LiteLLM](https://github.com/BerriAI/litellm) — Proxy to any LLM provider
- Any OpenAI-compatible endpoint

**Tier 2: Claude Code Agent** (automatic fallback, 10-20s) — If no API proxy is available, LiteClaw automatically creates a hidden Claude Code session to summarize responses. Since you already have Claude Code installed, this works without any extra setup. Set `SUMMARIZER_AGENT_MODEL` to choose a specific model, or leave empty for default.

**Tier 3: Raw Output** — If both tiers fail, responses are sent unfiltered. You can also force this with `/raw`.

At startup, LiteClaw probes the API endpoint. If it's unreachable, Tier 2 is pre-warmed automatically.

---

## Getting Your Bot Token

1. Open Telegram and search for `@BotFather`
2. Send `/newbot`
3. Choose a display name for your bot (e.g. "My Claude Bot")
4. Choose a username ending in `bot` (e.g. `my_claude_bot`)
5. BotFather will return a token like `123456789:ABCdef...`
6. Copy the token to `.env` as `BOT_TOKEN`

---

## Getting Your Chat ID

1. Open Telegram and search for `@userinfobot`
2. Send any message
3. It will reply with your numeric user ID — copy it to `.env` as `CHAT_ID`

---

## Security

**Bot token** — Stored only in `.env`, which is gitignored by default. Never hardcode your token in source code or share it publicly. Anyone with your bot token can send messages as your bot.

**Authentication** — Only messages from the Telegram user ID configured as `CHAT_ID` are processed. All other users are silently ignored. This is a single-user tool by design.

**tmux access** — LiteClaw has direct, unsandboxed access to your tmux session. It can inject arbitrary keystrokes. Secure your server with appropriate access controls — LiteClaw itself does not add any authentication beyond the Telegram chat ID check.

**`--dangerously-skip-permissions`** — This flag disables Claude Code's permission prompts, auto-approving all file writes, shell commands, and other actions. Only use this in trusted environments where you understand the implications.

**Network** — LiteClaw connects only to the Telegram API (for receiving and sending messages) and optionally to your local summarizer endpoint. No user data is sent to external servers beyond Telegram's own infrastructure.

---

## Dashboard

LiteClaw includes a built-in web dashboard for managing settings.

### Access

After starting LiteClaw, open: `http://localhost:7777`

### Features

- **Status**: See if Claude is busy or idle, API proxy availability
- **Model**: Switch summarizer model (Haiku/Sonnet/Opus) from dropdown
- **Raw Mode**: Toggle on/off with one click
- **Target**: Change tmux target without Telegram commands
- **Logs**: View recent activity

### Configuration

Set the port in `.env`:

```env
DASHBOARD_PORT=7777
```

Set to `0` to disable the dashboard.

---

## Troubleshooting

**"tmux session not found"**
The tmux session in `TMUX_TARGET` does not exist. Start Claude Code first:
```bash
tmux new-session -s claude 'claude --dangerously-skip-permissions'
```
Then update `TMUX_TARGET` in `.env` to match your session name.

**"Still processing / Use /cancel to abort"**
Claude is busy. LiteClaw queues your message but warns you. Send `/cancel` to interrupt the current task, then retry.

**No response from Claude**
Run `/status` to see Claude's current pane content. If you do not see the `❯` prompt, Claude may be waiting for input or stuck. If you use a custom shell prompt, add its pattern to `EXTRA_PROMPT_PATTERNS` in `.env`.

**Garbled or incomplete output**
Claude may have still been rendering when LiteClaw captured the response. Try `/raw` to see unfiltered output. If the issue is consistent, increase `SCROLLBACK_LINES` in `.env`.

**"Conflict: terminated by other getUpdates request"**
Another process is already polling the same bot token. Find and stop it:
```bash
ps aux | grep liteclaw.py
pkill -f liteclaw.py
```
Then restart LiteClaw.

**Summarizer requests timeout**
The summarizer proxy is slow or unreachable. Switch to raw mode with `/raw`, or verify the proxy is running and `SUMMARIZER_URL` is correct.

---

## Production Deployment

### Persistent tmux session

Run LiteClaw inside its own tmux session so it survives terminal disconnects:

```bash
tmux new-session -d -s liteclaw -c /path/to/liteclaw \
  '.venv/bin/python3 liteclaw.py'
```

Monitor it with:

```bash
tmux attach -t liteclaw
```

### systemd service

For automatic startup and restart on failure, create a systemd unit file:

```ini
[Unit]
Description=LiteClaw Telegram-Claude Bridge
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/liteclaw
ExecStart=/path/to/liteclaw/.venv/bin/python3 liteclaw.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Install and enable:

```bash
sudo cp liteclaw.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now liteclaw
```

---

## Disclaimer

LiteClaw is a personal project shared as-is for the community.

- **Use at your own risk.** The author is not responsible for any damage, data loss, or security issues arising from use of this software.
- This tool controls Claude Code via tmux. Ensure your server and tmux sessions are properly secured.
- Bot token and chat ID security is your responsibility. Never share your `.env` file.
- This project is not affiliated with, endorsed by, or sponsored by Anthropic.

---

## License

MIT
