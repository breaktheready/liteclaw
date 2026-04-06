# LiteClaw

Control Claude Code CLI remotely via Telegram. No API key needed.

[한국어](README_KO.md)

## What is LiteClaw?

LiteClaw is a lightweight bridge between Telegram and Claude Code CLI. It lets you interact with Claude Code from your phone — send messages, receive AI-summarized responses, transfer files, and monitor progress.

No additional API keys or subscriptions required. If you have Claude Code running in tmux, LiteClaw connects your Telegram to it.

## Features

- **Remote access** — Control Claude Code from anywhere via Telegram
- **AI summarization** — Responses cleaned up by Haiku before delivery (toggleable)
- **Busy detection** — Knows when Claude is working, queues your message
- **Progress updates** — See what Claude is doing while it works
- **File transfer** — Send and receive files through Telegram
- **Multi-target** — Switch between tmux sessions on the fly

## Quick Start

### Prerequisites

- Python 3.10+
- tmux 3.0+
- Claude Code CLI installed
- A Telegram bot token ([get one from @BotFather](https://t.me/BotFather))
- (Optional) OpenAI-compatible API endpoint for summarization

### Installation

```bash
git clone https://github.com/breaktheready/liteclaw.git
cd liteclaw
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configuration

```bash
cp .env.example .env
```

Edit `.env`:
```
BOT_TOKEN=your-telegram-bot-token
CHAT_ID=your-telegram-chat-id
TMUX_TARGET=claude:1
```

### Run

```bash
# Terminal 1: Start Claude Code
tmux new-session -s claude 'claude --dangerously-skip-permissions'

# Terminal 2: Start LiteClaw
source .venv/bin/activate
python3 liteclaw.py
```

## Commands

| Command | Description |
|---------|-------------|
| Any text | Send to Claude Code |
| `/status` | Show last 30 lines of Claude's output |
| `/target SESSION:WIN.PANE` | Switch tmux target |
| `/cancel` | Send Ctrl+C to Claude |
| `/sessions` | List all tmux sessions |
| `/escape` | Send Escape key |
| `/raw` | Toggle raw/summarized output |
| `/model MODEL` | Change summarizer model |
| `/get FILEPATH` | Download a file from server |
| Send a file | Upload to server and relay to Claude |
| Send a photo | Save photo and relay path to Claude |

## How It Works

```
You (Telegram) → LiteClaw → tmux send-keys → Claude Code CLI
                                                    ↓
You (Telegram) ← Haiku summary ← capture-pane ← Response
```

1. You send a message on Telegram
2. LiteClaw checks if Claude is idle or busy
3. Injects your message into the tmux pane via `send-keys`
4. Polls `capture-pane` every 1.5s until response stabilizes
5. Optionally summarizes via Haiku (or any OpenAI-compatible API)
6. Sends the clean response back to Telegram

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `BOT_TOKEN` | (required) | Telegram bot token from @BotFather |
| `CHAT_ID` | (required) | Your Telegram chat ID |
| `TMUX_TARGET` | `claude:1` | tmux target pane |
| `SUMMARIZER_URL` | `http://localhost:8080/v1` | OpenAI-compatible API endpoint |
| `SUMMARIZER_MODEL` | `claude-haiku-4-5` | Model for summarization |
| `SCROLLBACK_LINES` | `500` | Lines to capture from tmux |
| `INTERMEDIATE_INTERVAL` | `10` | Seconds between progress updates |
| `STAGING_DIR` | `~/liteclaw-files` | Directory for file uploads |
| `EXTRA_PROMPT_PATTERNS` | (empty) | Comma-separated regex patterns for custom prompt detection |

## Summarizer Setup

LiteClaw works without a summarizer (`/raw` mode). For AI-powered response cleanup, point `SUMMARIZER_URL` to any OpenAI-compatible API:

- [claude-max-api-proxy](https://github.com/1mancrew/claude-max-api-proxy) — Use your Claude Max subscription
- [LiteLLM](https://github.com/BerriAI/litellm) — Proxy to any LLM provider
- Any OpenAI-compatible endpoint

## Troubleshooting

**"Conflict: terminated by other getUpdates request"**
Another process is using the same bot token. Stop it first.

**No response from Claude**
Check if Claude is at the `❯` prompt: `/status`

**Garbled output in messages**
Make sure you're not in `/raw` mode. The summarizer cleans up terminal noise.

**"tmux session not found"**
Start Claude Code in tmux first: `tmux new-session -s claude`

## License

MIT
