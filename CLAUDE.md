# Technical Reference: LiteClaw

A lightweight Telegram bot that bridges messages between Telegram and a Claude Code CLI session running in tmux. Uses tmux command injection (`send-keys`), pane capture (`capture-pane`), and LLM summarization to deliver clean, readable responses back to Telegram.

---

## Architecture

```
[Telegram Bot] <──> [liteclaw.py daemon] <──> [tmux pane: claude:1]
                         │
                         ├─→ [Tier 1: API proxy summarizer]
                         ├─→ [Tier 2: Claude Code agent summarizer]
                         └─→ [Tier 3: Raw output fallback]
```

**Single file codebase**: `liteclaw.py` (~2700 lines)

**Request flow**:
1. User sends text/file/photo to Telegram bot
2. LiteClaw receives message via python-telegram-bot 20.6 polling
3. Acquires busy lock (rejects concurrent requests)
4. Takes pre-snapshot of tmux pane
5. Injects message into tmux pane via `load-buffer` + `paste-buffer`
6. Polls `capture-pane` every 1.5s until response stabilizes
7. Extracts response via pre/post snapshot diff
8. Unless raw mode: summarizes via 3-tier fallback chain
9. Splits at 4000 chars, delivers to Telegram (3x retry + direct API fallback)
10. Logs to conversation history (JSONL)
11. Schedules follow-up check (30s) to catch resumed output

---

## Tech Stack

- **Python**: 3.12+ (venv at `.venv/`)
- **Bot framework**: python-telegram-bot==20.6
- **HTTP client**: httpx (async, for direct Telegram API fallback + summarizer)
- **Environment**: python-dotenv
- **Scheduler**: APScheduler (cron jobs)
- **IPC**: tmux (no network calls — all local)
- **Summarizer**: Claude models via OpenAI-compatible API proxy

---

## Configuration

All secrets and tuning parameters in `.env` (gitignored). See `.env.example` for template.

### Required
```
BOT_TOKEN=<telegram_bot_token>
CHAT_ID=<your_telegram_chat_id>
```

### Optional (with defaults)
```
TMUX_TARGET=claude:1                    # tmux target pane
SUMMARIZER_URL=http://localhost:8080/v1 # OpenAI-compatible API endpoint
SUMMARIZER_MODEL=claude-sonnet-4-6      # Model for Tier 1 summarization
SUMMARIZER_AGENT_MODEL=                 # Optional Tier 2 agent model override
SCROLLBACK_LINES=500                    # capture-pane scrollback depth
DASHBOARD_PORT=7777                     # Web dashboard port (0=disabled)
INTERMEDIATE_INTERVAL=10                # Status update cadence (seconds)
STAGING_DIR=~/liteclaw-files            # File upload staging directory
HISTORY_FILE=~/.liteclaw-history.jsonl  # Conversation history location
HISTORY_RECALL_LIMIT=50                 # Max entries for /recall
PIPE_LOG_DIR=/tmp                       # pipe-pane log location
PROXY_DIR=~/max_api_proxy               # API proxy dir for auto-recovery
EXTRA_PROMPT_PATTERNS=                  # Comma-separated custom prompt regex
```

**Environment variables override defaults** — if not in `.env`, hardcoded defaults used.

---

## Key Components

### LiteClaw Class

Main bot orchestrator. Stateful singleton.

**State**:
- `target`: Current tmux target (e.g. `claude:1`)
- `busy`: Lock to prevent concurrent message handling
- `raw_mode`: Boolean — if True, send unprocessed output; if False, summarize
- `_pipe_active`: Track whether pipe-pane is running
- `_api_available`: Tri-state (None=unknown, True, False) — Tier 1 probe result
- `_agents`: Multi-agent registry dict
- `_cron_jobs`: Scheduled job list
- `_cron_running`: Set of active job IDs (overlap prevention)

### DashboardHandler

HTTP handler for the web dashboard (default port 7777).
- JSON API: `/api/config`, `/api/status`, `/api/logs`
- Live config editing: model, raw mode, tmux target
- No persistence — all changes in-memory until restart

### Prompt Detection

`has_prompt(content: str) -> bool`

Detects Claude Code prompt and confirmations by scanning last 15 lines:

```python
PROMPT_PATTERNS = [
    r"^\s*❯[\s\xa0]",            # Claude Code prompt
    r"[\w@\.~:\-/]+[\$#]\s*$",   # Shell prompt (user@host:~/path$)
    r"\[Y/n\]\s*$",              # Tool use confirmation
    r"\[y/N\]\s*$",              # Confirmation prompt (default no)
    r"Do you want to proceed",    # Various confirmations
]
```

**Critical**: Normalizes non-breaking spaces (`\xa0` → space) before matching. Claude Code TUI uses nbsp in prompt lines.

`is_idle_prompt(content: str) -> bool`

Stricter version — returns True only when prompt is visible AND no activity spinner detected in last 5 non-empty lines. Used for completion detection to prevent premature "done" during tool calls.

**Activity patterns**: 22+ Claude Code spinner labels (Doing, Computing, Channelling, Nesting, etc.) + `(thinking)` indicator. Requires spinner character prefix (✻✶✽✢·●* etc.).

### Output Cleaning

`clean_output(text: str) -> str`

Strips ANSI codes, OSC sequences, and Claude TUI noise:
- ANSI escapes: `\x1b[...m`
- OSC sequences: `\x1b]...\x07`
- Status bar: `[OMC]`, permission mode indicators
- UI noise: Keel mascot, separator lines, box-drawing characters
- Confirmation prompts: hints to expand/cycle modes

### Polling & Diff Extraction

`_poll_response(bot) -> str`

Polls `capture-pane` every 1.5s with stability detection:
1. Takes pre-snapshot before sending message
2. Waits 2s, then enters poll loop
3. Each poll: captures pane, compares with previous (raw content)
4. If `prompt_count >= 5` (consecutive idle polls) AND `elapsed >= 5s`: exit loop
5. Every 4s: Telegram typing indicator
6. Every 10s (growing to 60s): status update with last 5 meaningful lines
7. Timeout: 45s force-deliver (configurable MAX_WAIT)
8. Calls `_extract_response()` for pre/post diff

**Why stability detection**: Claude Code continues to render after output arrives (animations, re-renders). Multiple consecutive identical captures = rendering complete.

---

## Telegram Commands

All commands require authorization (chat ID matches `CHAT_ID`).

### Core Commands
| Command | Args | Action |
|---------|------|--------|
| `/start`, `/help` | — | Show commands and current config |
| `/status` | — | Last 30 lines of tmux pane (raw) |
| `/target` | `SESSION:WIN.PANE` | Change tmux target — restarts pipe-pane |
| `/cancel` | — | Send Ctrl+C to current pane |
| `/escape` | — | Send Escape key |
| `/raw` | — | Toggle raw mode ON/OFF |
| `/model` | `MODEL_NAME` | Change summarizer model |
| `/sessions` | — | List all active tmux sessions |
| `/get` | `FILEPATH` | Download file from server (max 50MB) |
| `/recall` | `[N\|keyword]` | Search conversation history |

### Multi-Agent Commands
| Command | Args | Action |
|---------|------|--------|
| `/agents` | — | List all registered agents |
| `/agent new` | `NAME PATH` | Create new Claude Code agent in tmux |
| `/agent status` | — | Detailed status with pane preview |
| `/agent remove` | `NAME` | Kill agent session and unregister |
| `/assign` | `NAME TASK` | Send task to agent and poll response |

### Cron Commands
| Command | Args | Action |
|---------|------|--------|
| `/cron list` | — | Show all scheduled jobs |
| `/cron add` | `ID CRON(5) PROJECT MSG` | Create cron job |
| `/cron remove` | `ID` | Delete and unschedule |
| `/cron enable/disable` | `ID` | Toggle job execution |
| `/cron run` | `ID` | Manual trigger (test) |
| `/cron log` | `ID` | Show last run time/status |

### File Handlers
| Input | Action |
|-------|--------|
| Text message | Relay to Claude Code, poll and return response |
| Document upload | Save to staging dir, inline small text (<50KB) |
| Photo upload | Save with timestamp, relay path to Claude |

---

## Multi-Agent Architecture

LiteClaw acts as **org lead** — it manages independent Claude Code instances (agents) running in separate tmux sessions.

### How It Works
```
[LiteClaw (org lead)]
    ├── tmux session: claude      ← main Claude Code (default target)
    ├── tmux session: agent-web   ← project agent (e.g. frontend work)
    ├── tmux session: agent-api   ← project agent (e.g. backend work)
    └── tmux session: cron-xyz    ← ephemeral cron job session
```

### Agent Lifecycle
1. `/agent new NAME PATH` → creates `tmux new-session -s agent-{NAME}`, starts Claude Code
2. Waits 20s for Claude Code prompt to appear
3. Status tracked: `starting | idle | busy | dead | error`
4. `/assign NAME TASK` → snapshots pane, sends task, polls for response
5. `/agent remove NAME` → kills tmux session, removes from registry

### Agent Persistence
- Registry: `.agents.json` (same directory as liteclaw.py)
- On startup: loads from disk, reconciles with live tmux sessions (marks dead agents)
- Survives LiteClaw restarts

### Cron Job Execution
1. APScheduler triggers at configured cron time
2. Creates ephemeral tmux session `cron-{job_id}`
3. Spawns Claude Code, waits 30s for prompt
4. Sends message, polls response (per-job timeout, default 600s)
5. Summarizes, delivers to Telegram
6. Updates `last_run` / `last_status` in `.cron_jobs.json`

---

## 3-Tier Summarizer

Ensures response delivery even when infrastructure is degraded.

| Tier | Method | Condition |
|------|--------|-----------|
| **1** | API proxy (`SUMMARIZER_URL`) | Default — fastest, cleanest |
| **2** | Hidden Claude Code agent session | If Tier 1 unavailable or fails |
| **3** | Raw output (no processing) | If Tier 1 and 2 both fail |

**Tier 2 details**: Spawns a hidden tmux session (`liteclaw-summarizer`) running Claude Code. Sends raw output as a summarization prompt. Slower but works without API proxy.

**Fallback is automatic** — no user intervention needed. Bot remains functional at all tiers.

---

## Auto-Recovery Mechanisms

### API Proxy Recovery
- On Tier 1 failure: runs `docker compose up -d` in the proxy directory
- Retries connection after restart
- Notifies user via Telegram on successful recovery

### Session Auth Recovery (401)
- Detects "401" + auth keywords in tmux pane output
- Sends `/login` command to Claude Code session
- Extracts OAuth URL from pane output and forwards to Telegram
- User taps link on phone to approve (2-minute approval window)
- Confirms re-authentication and notifies via Telegram

### Auth Heartbeat
- Runs `claude auth status` every 30 minutes (first check at 5 min after startup)
- If auth expired: initiates recovery flow automatically
- Prevents silent session death from undetected token expiry

### Telegram Send Recovery
- 3x retry with exponential backoff on send failures
- Falls back to direct `httpx` API call (bypassing bot framework)
- Last resort: logs error, does not crash

---

## Guardrails & Safety

### Authentication
- **Single-user only**: All handlers check `update.effective_chat.id == CHAT_ID`
- No RBAC or multi-user support by design
- Unauthorized messages are silently ignored

### Concurrency Protection
- `self.busy` lock prevents concurrent request processing
- If busy: rejects with status message, does NOT queue silently
- Cron: `_cron_running` set prevents overlapping job executions

### Resource Limits
- File download: 50MB max (`/get` command)
- Large input (>500 chars): auto-saves to temp file, tells Claude to read it
- Message splitting: 4000 chars per Telegram message (line-aware)
- Scrollback: configurable depth (default 500 lines)

### Timeouts
| Operation | Default | Purpose |
|-----------|---------|---------|
| API summarization | 60s | Prevent hung HTTP calls |
| Agent summarization | 30s | Internal polling limit |
| Response polling | 45s (MAX_WAIT) | Force-deliver incomplete output |
| Cron execution | 600s per job | Prevent runaway jobs |
| Follow-up monitoring | 30 min | Catch resumed output |
| OAuth URL approval window | 2 min | Wait for user to tap login link |
| Auth heartbeat | 30 min | Detect expired sessions early |

### Process Management
- **Single instance only**: Same bot token cannot be polled by two processes simultaneously
- Check before starting: `pgrep -f liteclaw.py`
- Port conflict: Dashboard uses port 7777 — `fuser -k 7777/tcp` if needed
- **Never run duplicate instances** — causes Telegram polling conflicts

---

## Known Issues & Gotchas

### Non-breaking Spaces in Prompt Detection
Claude Code TUI uses Unicode non-breaking space (`\xa0`) in prompt lines. Regex matching can fail if `\xa0` appears where `\s` won't match it.

**Mitigation**: `has_prompt()` normalizes all lines with `.replace("\xa0", " ")` before matching.

### pipe-pane vs capture-pane
pipe-pane writes raw ANSI garbage to log files. **Never use for content extraction** — always use `capture-pane`.

**Why**: pipe-pane captures happen asynchronously in tmux; race conditions occur. capture-pane is synchronous and reliable.

### Telegram Message Size Limit
Telegram enforces 4096 character limit. Auto-splits at 4000 chars (96-char safety margin) respecting line boundaries. Long responses split into numbered chunks `[1/N], [2/N]`.

### Single Bot Token Constraint
Only one process can poll a Telegram bot token at a time. If another service (e.g. OpenClaw) uses the same token, stop it first.

### send-keys Special Character Crash
Direct `tmux send-keys -l` crashes on quotes, newlines, and special characters.

**Solution**: Uses `load-buffer` + `paste-buffer` via temp file instead. Handles all character types safely.

### Completion Detection Tradeoffs
`prompt_count >= 5` means ~7.5s of confirmed idle before declaring "done". This is intentionally conservative — prevents delivering incomplete responses during tool calls, but adds latency for short responses.

---

## Running

### Prerequisites
1. **Python 3.10+** with venv
2. **tmux 3.0+** installed
3. **Claude Code CLI** installed, in PATH, and authenticated
4. `.env` configured with `BOT_TOKEN` and `CHAT_ID`
5. **(Recommended)** OpenAI-compatible API proxy for Tier 1 summarization (e.g. Docker-based proxy at `SUMMARIZER_URL`)
6. **(Optional)** Docker — only if API proxy runs in container (enables auto-recovery via `PROXY_DIR`)

### Infrastructure: API Proxy
LiteClaw's Tier 1 summarizer depends on an OpenAI-compatible API proxy.
- If down: auto-recovery attempts `docker compose up -d`
- Manual check: `curl -s http://localhost:8080/v1/models`
- **Degraded without proxy** — Tier 2/3 still work but quality drops

### Startup Sequence

```bash
# Terminal 1: Start Claude Code in tmux
tmux new-session -s claude 'claude --dangerously-skip-permissions'

# Terminal 2: Start LiteClaw
cd /path/to/liteclaw
source .venv/bin/activate
python3 liteclaw.py
```

Or as background process:
```bash
nohup .venv/bin/python3 liteclaw.py > /tmp/liteclaw-stdout.log 2>&1 &
```

**Startup flow**:
1. Load `.env`, validate required vars
2. Check target tmux session exists
3. Load agent/cron registries, reconcile with live tmux
4. Probe API summarizer availability (5s timeout)
5. Start dashboard (port 7777) in background thread
6. Schedule cron jobs with APScheduler
7. Start pipe-pane logging
8. Begin Telegram bot polling (infinite loop)

### Shutdown
- Foreground: `Ctrl+C` (graceful exit)
- Background: `pkill -f 'python3 liteclaw.py'`

---

## Debugging

### Check LiteClaw Logs
```bash
tail -f /tmp/liteclaw-stdout.log    # if running as background process
tmux capture-pane -t bridge -p -S -50  # if running in tmux
```

### Check Claude Pane Content
```bash
tmux capture-pane -t claude:1 -p -S -20
```

### Test Prompt Detection
```python
from liteclaw import has_prompt
import subprocess
result = subprocess.run(
    ["tmux", "capture-pane", "-t", "claude:1", "-p", "-S", "-30"],
    capture_output=True, text=True
)
print("Prompt detected:", has_prompt(result.stdout))
```

### Test API Proxy
```bash
curl -s http://localhost:8080/v1/models
# Should return JSON with model list
```

### Manual tmux Injection Test
```bash
tmux send-keys -t claude:1 -l "ls -la"
tmux send-keys -t claude:1 Enter
sleep 2
tmux capture-pane -t claude:1 -p -S -10
```

### Check Agent Status
```bash
tmux list-sessions | grep agent-    # list agent sessions
cat .agents.json | python3 -m json.tool  # agent registry
```

### Check Cron Jobs
```bash
cat .cron_jobs.json | python3 -m json.tool  # job config
tmux list-sessions | grep cron-      # active cron sessions
```

---

## Performance Notes

### Polling Interval
Default: 1.5s between captures. Adjustable via `POLL_INTERVAL` in code.
- Lower = faster detection, higher CPU
- Higher = slower detection, lower CPU

### Completion Detection
Uses `is_idle_prompt()` — requires prompt visibility AND absence of activity spinners. Triggers when `prompt_count >= 5` AND `elapsed >= 5s`.

**Post-delivery check-back**: After sending response, `_followup_edit()` monitors for 30 min. If Claude produces additional output (>10% longer), sends follow-up message. Prevents silent response loss.

### Stability Threshold
Default: 3 consecutive unchanged captures. Used alongside `is_idle_prompt()` for belt-and-suspenders completion detection.

### Status Update Cadence
Starts at 10s, grows to 60s for long-running tasks. Sends last 5 meaningful lines as progress update.

---

## Extension Points

### Adding Custom Prompt Patterns
In `.env`:
```
EXTRA_PROMPT_PATTERNS=my custom>$,another pattern.*$
```

Or edit `PROMPT_PATTERNS` in liteclaw.py.

### Changing Summarizer Behavior
Edit `SUMMARIZE_PROMPT` global variable to adjust how the LLM formats output.

Currently: "Extract meaningful response, discard terminal noise, use Telegram-friendly Markdown"

### Custom Message Formatting
`format_for_telegram()` converts CLI output to Telegram HTML:
- Code blocks: triple backticks → `<pre><code>`
- Inline code: backticks → `<code>`
- Bold: `**text**` → `<b>`

### File Transfer Customization
`STAGING_DIR` configurable via `.env`. Photo naming: `photo_YYYYMMDD_HHMMSS.ext`

---

## Local Filesystem

| Path | Purpose |
|------|---------|
| `.agents.json` | Multi-agent registry (persists across restarts) |
| `.cron_jobs.json` | Cron job config and status |
| `~/.liteclaw-history.jsonl` | Conversation history |
| `~/liteclaw-files/` | Uploaded file staging area |
| `/tmp/liteclaw_input_*.txt` | Long user input buffer |
| `/tmp/liteclaw_buf_*.txt` | tmux send buffer (temp) |
| `/tmp/liteclaw_*.log` | pipe-pane logs |

---

## Dependencies

From `requirements.txt`:
```
python-telegram-bot==20.6
httpx
python-dotenv
```

APScheduler is imported but should be added if using cron features.

Install:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Design Principles

- **Zero-overhead bridge**: tmux is the "API" — no intermediate services required
- **Single-file**: Everything in one Python file for easy deployment
- **Graceful degradation**: Every feature has a fallback (3-tier summarizer, retry logic)
- **Single-instance**: Not designed for multi-user — one bot token, one user, one process
- **Conservative completion**: Prefer delayed delivery over incomplete responses
