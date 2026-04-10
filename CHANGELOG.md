# Changelog

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
