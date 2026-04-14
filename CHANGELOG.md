# Changelog

## v0.4.3 (2026-04-14) — CLAUDE.md, Auth Heartbeat & Security Hardening

### Added
- **CLAUDE.md**: Comprehensive technical reference for Claude Code integration
  - Architecture overview, component docs, multi-agent guardrails
  - 3-tier summarizer, auto-recovery, debugging guide
  - Performance tuning, extension points, filesystem layout
- **Auth heartbeat**: Periodic `claude auth status` check every 30 minutes
  - Detects expired sessions before they cause 401 errors
  - Auto-initiates recovery flow when auth expires
- **OAuth URL forwarding**: On 401, sends `/login` and forwards the OAuth URL to Telegram
  - User taps the link on their phone to re-authenticate (2-min window)
  - No more Node.js/Playwright dependency for OAuth auto-approval
- **`PROXY_DIR` env variable**: API proxy directory is now configurable (was hardcoded)

### Fixed
- **Security**: Removed hardcoded personal path in `_recover_proxy()` — now uses `PROXY_DIR` env var with `~/max_api_proxy` default
- **Security**: Replaced personal project path in `/cron` help example with generic placeholder

### Changed
- `.gitignore`: Added `.agents.json`, `.cron_jobs.json`, `.whale_state.json` to prevent accidental commit of runtime state files containing personal data
- `.env.example`: Added `PROXY_DIR` entry

### Removed
- **`_auto_approve_oauth()`**: Replaced by OAuth URL forwarding to Telegram (no more Playwright/Node.js needed)

## v0.4.2 (2026-04-12) — Response Delivery Overhaul

### Fixed
- Premature response capture during Claude tool calls (prompt_count tuning)
- `tmux send-keys` crash on special characters (quotes, newlines, Unicode)
- Incorrect response extraction when user echo not found in scrollback
- Previous follow-up task overwriting current message on new input
- Infinite follow-up loop when pane content continuously changes

### Added
- **Follow-up edit**: Delivered messages auto-update as Claude continues working
  - Continuous monitoring loop (5 min max)
  - Late edit notification (once, after 60s)
  - Previous follow-up cancelled on new message
- **Conversation history**: All exchanges logged to `~/.liteclaw-history.jsonl`
- **`/recall` command**: Search/summarize past conversations with keyword filter
- **Pre-snapshot diff**: Reliable response extraction by comparing pane before/after
- **Status message cleanup**: "Sent. Waiting..." deleted before final delivery

### Removed
- `_pane_watcher` background task (replaced by follow-up edit)
- `_checkback_deliver` and `_judge_new_content` (obsolete)

### Changed
- `send_keys()`: Now uses `tmux load-buffer` + `paste-buffer` for safety
- `MAX_WAIT`: Set to 45s (was infinite)
- Completion detection: `prompt_count >= 5` with last-3-line stability comparison

## v0.4.1 (2026-04-11)

### Fixed
- **Premature completion detection during tool calls**: `_poll_response()` no longer detects "done" when Claude Code is mid-tool-call
  - Added `is_idle_prompt()` — checks prompt visibility AND absence of activity spinners simultaneously
  - Expanded `_ACTIVITY_PATTERNS` to cover all 22 known Claude Code spinner labels (Doing, Computing, Channelling, Nesting, Brewing, etc.) plus `(thinking)` indicator
  - Changed completion criteria from `prompt_count >= 3` to `prompt_count >= 5` (~7.5s of continuous idle required)
  - Added `elapsed >= 5` minimum wait to prevent premature exit
- **Missed response delivery**: When liteclaw sent an incomplete response (due to premature completion), the actual final response was never delivered
  - Added `_checkback_deliver()` — 30s after delivery, re-checks pane for new content; sends follow-up message if genuinely new output found (>10% longer)
  - Comparison is raw-to-raw (pre-summarization) to avoid length mismatch false positives
- **`_background_deliver()` now uses `is_idle_prompt()`** instead of `has_prompt()` for consistent idle detection
- **Summarizer agent poll** also updated to use `is_idle_prompt()` for consistency
- **Cleaned up 145 stale OMC todo files** that were triggering Stop hook and blocking Claude Code responses

## v0.4.0 (2026-04-10)

### Added
- **Multi-Agent Architecture**: LiteClaw as org lead with independent peer agents
  - `/agents` — list all managed agent sessions
  - `/agent new <name> <path>` — create new agent with Claude Code in tmux
  - `/agent status` — detailed agent status with pane preview
  - `/agent remove <name>` — remove agent session
  - `/assign <agent> <task>` — dispatch task to agent, poll response, relay to Telegram
  - Agent registry persists via `.agents.json`
- **Unified Notification Module** (`notify.py`)
  - All notifications route through summarizer for clean Telegram formatting
  - Raw fallback when summarizer is unavailable
  - Standalone module usable by whale_monitor, cron scripts, etc.
- **OKL-ear Completion Alert**: Telegram notification after daily report generation
- **401 Auto-Recovery**
  - Detects max-api-proxy downtime → auto `docker compose up -d`
  - Detects summarizer session 401 → auto `/login` re-authentication
  - Telegram notification on successful recovery

### Changed
- `whale_monitor.py` now uses `notify.py` for all Telegram messaging
- Summarizer Tier 1 distinguishes connection errors from other failures for targeted recovery

## v0.3.0 (2026-04-08)

### Added
- Web dashboard at `http://localhost:7777` for settings management
  - View/change summarizer model, raw mode, tmux target
  - Real-time status monitoring (busy/idle, API availability)
  - Recent logs viewer
- Enhanced debug logging in `_summarize` method
  - Entry logging with state details
  - Success logging with result size
  - Warning-level logging for failures

### Fixed
- Summarizer API timeout increased from 15s to 60s (prevents premature Tier 1 failures)
- Removed permanent API disabling on failure (now retries every call)
- Tier 1 failure no longer permanently falls back to raw output

### Changed
- Unconditional Tier 1 API attempt (removed `_api_available` caching logic)

## v0.2.0 (2026-04-05)

### Added
- 3-tier summarization fallback (API proxy → Claude agent → raw)
- Documentation for summarizer fallback in READMEs

## v0.1.0 (2026-04-04)

### Added
- Initial release
- Telegram ↔ Claude Code bridge via tmux
- File transfer (upload/download)
- Response summarization via local Claude proxy
- Multi-target tmux support
