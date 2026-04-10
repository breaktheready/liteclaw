# LiteClaw

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
- **Multi-agent orchestration** — LiteClaw acts as org lead, spinning up independent peer agents in separate tmux sessions. New commands: `/agents`, `/agent new|status|remove`, `/assign`. Agent registry persists across restarts.
- **Auto-recovery** — Detects and recovers from API proxy downtime automatically. Re-authenticates Claude Code sessions on 401 errors and notifies you via Telegram when back online.
- **Unified notifications** — All Telegram messages route through a single `notify.py` module with summarizer cleanup. Falls back to raw output if the summarizer is unavailable.

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

---

## Configuration

All settings are controlled via `.env`. Copy `.env.example` and edit as needed.

| Variable | Default | Description |
|----------|---------|-------------|
| `BOT_TOKEN` | (required) | Telegram bot token from @BotFather |
| `CHAT_ID` | (required) | Your numeric Telegram user ID |
| `TMUX_TARGET` | `claude:1` | Target tmux session and window (`SESSION:WINDOW.PANE`) |
| `SUMMARIZER_URL` | `http://localhost:8080/v1` | OpenAI-compatible API endpoint for summarization |
| `SUMMARIZER_MODEL` | `claude-haiku-4-5` | Model to use for response cleanup |
| `SCROLLBACK_LINES` | `500` | Number of tmux history lines to capture per poll |
| `INTERMEDIATE_INTERVAL` | `10` | Seconds between progress updates while Claude works |
| `STAGING_DIR` | `~/liteclaw-files` | Directory where uploaded files are saved on the server |
| `EXTRA_PROMPT_PATTERNS` | (empty) | Comma-separated regex patterns for custom prompt detection |

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
| Send a document | Upload a file to `STAGING_DIR` and relay its path (and contents for small files) to Claude |
| Send a photo | Upload a photo to `STAGING_DIR` and relay the path to Claude for vision tasks |

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
- [claude-max-api-proxy](https://github.com/1mancrew/claude-max-api-proxy) — Use your Claude Max subscription
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
