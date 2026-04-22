#!/usr/bin/env python3
"""LiteClaw — Control Claude Code CLI remotely via Telegram.

A lightweight bridge that relays messages between Telegram and a Claude Code
session running in tmux. No API key needed — uses your existing Claude Code
subscription through tmux send-keys + capture-pane.

Usage:
    1. Start Claude Code in tmux:
       tmux new-session -s claude 'claude --dangerously-skip-permissions'

    2. Configure .env:
       BOT_TOKEN=your-telegram-bot-token
       CHAT_ID=your-telegram-chat-id

    3. Run:
       liteclaw
"""

import asyncio
import hashlib
import html
import json as _json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from functools import partial
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =============================================================================
# Configuration
# =============================================================================

load_dotenv()

# Config values are loaded leniently so `import liteclaw` works even with an
# unconfigured/placeholder .env (e.g. right after `setup.sh`, or when a test
# harness imports the module to smoke-check). Actual validation happens in
# main() before the bot starts.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
try:
    CHAT_ID = int(os.environ.get("CHAT_ID", "0"))
except ValueError:
    CHAT_ID = 0
TMUX_TARGET = os.environ.get("TMUX_TARGET", "claude:1")

# Summarizer config (local Claude proxy)
SUMMARIZER_URL = os.environ.get("SUMMARIZER_URL", "http://localhost:3456/v1")
SUMMARIZER_MODEL = os.environ.get("SUMMARIZER_MODEL", "claude-sonnet-4-6")
SUMMARIZER_AGENT_MODEL = os.environ.get("SUMMARIZER_AGENT_MODEL", "")  # model for Tier 2 agent
SUMMARIZER_AGENT_SESSION = "liteclaw-summarizer"  # hidden tmux session for Tier 2

POLL_INTERVAL = 1.5      # seconds between capture-pane polls
STABILITY_THRESHOLD = 3   # consecutive unchanged polls = response done
MAX_WAIT = 45             # seconds — force deliver to prevent infinite wait
SCROLLBACK_LINES = int(os.environ.get("SCROLLBACK_LINES", "500"))
TG_MAX_LEN = 4000         # telegram message length (leave buffer from 4096)

# pipe-pane log directory
PIPE_LOG_DIR = os.environ.get("PIPE_LOG_DIR", "/tmp")

# Dashboard
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "7777"))

# Intermediate streaming config
INTERMEDIATE_INTERVAL = int(os.environ.get("INTERMEDIATE_INTERVAL", "10"))
INTERMEDIATE_MIN_CHARS = 200  # minimum new chars to trigger intermediate update

# File transfer config
STAGING_DIR = Path(os.environ.get("STAGING_DIR", os.path.expanduser("~/liteclaw-files")))

# Conversation history
HISTORY_FILE = Path(os.environ.get("HISTORY_FILE", os.path.expanduser("~/.liteclaw-history.jsonl")))
HISTORY_RECALL_LIMIT = int(os.environ.get("HISTORY_RECALL_LIMIT", "50"))  # max entries for /recall

# OpenClaw-style memory layout: per-day transcripts + per-day markdown summaries
# + rolling strategic compact + synthesized startup primer.
LITECLAW_DIR = Path(os.environ.get("LITECLAW_DIR", os.path.expanduser("~/.liteclaw")))
LITECLAW_TRANSCRIPTS = LITECLAW_DIR / "transcripts"
LITECLAW_MEMORY = LITECLAW_DIR / "memory"
LITECLAW_STRATEGIC = LITECLAW_MEMORY / "strategic.md"
LITECLAW_PRIMER = LITECLAW_DIR / "primer.md"
LITECLAW_SESSIONS = LITECLAW_DIR / "sessions.json"

# Boot notification (sent to Telegram once after the bot is fully initialized)
BOOT_NOTIFY = os.environ.get("BOOT_NOTIFY", "1").lower() not in ("0", "false", "no", "off")

# Claude Code working directory (where start.sh launches claude). Used to
# locate ~/.claude/projects/<encoded-cwd>/ for resume-state detection.
CLAUDE_CWD = Path(os.environ.get("CLAUDE_CWD", os.path.expanduser("~")))
PRIMER_RECENT_TURNS = int(os.environ.get("PRIMER_RECENT_TURNS", "20"))

# JSONL-based response extraction: reads Claude Code's own session log at
# ~/.claude/projects/<encoded-cwd>/<liteclaw_session_id>.jsonl for clean,
# structured responses instead of scraping the tmux pane (which leaks TUI
# chrome like "Thinking (Crystalizing)" and truncates long answers at the
# scroll-back boundary). Setting USE_JSONL_RESPONSE=0 disables this path.
USE_JSONL_RESPONSE = os.environ.get("USE_JSONL_RESPONSE", "1").lower() not in ("0", "false", "no", "off")
# Suppress mid-poll status edits while waiting for the final response — they
# were the source of the "Thinking (Crystalizing)" garbled messages on
# Telegram. Users can re-enable via SHOW_POLLING_STATUS=1 for debugging.
SHOW_POLLING_STATUS = os.environ.get("SHOW_POLLING_STATUS", "0").lower() not in ("0", "false", "no", "off")

# v0.5.0 UX Overhaul — OpenClaw/Hermes-inspired
# F1 CLI Mirror
MIRROR_ENABLED = os.environ.get("MIRROR_ENABLED", "false").lower() == "true"
MIRROR_DEBOUNCE = float(os.environ.get("MIRROR_DEBOUNCE", "10"))
MIRROR_POLL_INTERVAL = float(os.environ.get("MIRROR_POLL_INTERVAL", "3"))

# F2 Draft Streaming
DRAFT_STREAM_ENABLED = os.environ.get("DRAFT_STREAM_ENABLED", "true").lower() == "true"
DRAFT_STREAM_INTERVAL = float(os.environ.get("DRAFT_STREAM_INTERVAL", "4"))

# F3 Reasoning Lane
REASONING_LANE_ENABLED = os.environ.get("REASONING_LANE_ENABLED", "true").lower() == "true"
REASONING_PREFIX = os.environ.get("REASONING_PREFIX", "🧠")

# F4 Interactive prompts
INTERACTIVE_AUTO_YN = os.environ.get("INTERACTIVE_AUTO_YN", "true").lower() == "true"
INTERACTIVE_FREEFORM = os.environ.get("INTERACTIVE_FREEFORM", "true").lower() == "true"
DOWN_KEY_DELAY = float(os.environ.get("DOWN_KEY_DELAY", "0.35"))

# F6 Skills
LITECLAW_HOME = Path(os.environ.get("LITECLAW_HOME", os.path.expanduser("~/.liteclaw")))
SKILLS_PATH = Path(os.environ.get("SKILLS_PATH", str(LITECLAW_HOME / "skills")))
SKILLS_HOT_RELOAD = os.environ.get("SKILLS_HOT_RELOAD", "true").lower() == "true"
SKILLS_NATIVE_MENU = os.environ.get("SKILLS_NATIVE_MENU", "true").lower() == "true"
CONFIG_PATH = LITECLAW_HOME / "config.json"

# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    datefmt="%H:%M:%S",
)
log = logging.getLogger("liteclaw")

# =============================================================================
# Prompt Detection
# =============================================================================

PROMPT_PATTERNS = [
    r"^\s*❯[\s\xa0]",            # Claude Code prompt (❯ at line start, followed by whitespace)
    r"[\w@\.~:\-/]+[\$#]\s*$",   # shell prompt (user@host:~/path$ or root#)
    r"\[Y/n\]\s*$",              # tool use confirmation
    r"\[y/N\]\s*$",              # confirmation prompt (default no)
    r"Do you want to proceed",    # various confirmations
]

# Add user-defined patterns from .env
_extra = os.environ.get("EXTRA_PROMPT_PATTERNS", "")
if _extra.strip():
    PROMPT_PATTERNS.extend(p.strip() for p in _extra.split(",") if p.strip())

_PROMPT_RE = re.compile("|".join(f"(?:{p})" for p in PROMPT_PATTERNS))

# =============================================================================
# Helpers
# =============================================================================

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
NOISE_PATTERNS = [
    r"\[OMC\]",                      # OMC status bar
    r"bypass permissions",            # permission mode indicator
    r"Remote Control",                # remote control indicator
    r"─{5,}|━{5,}|═{5,}",           # box-drawing separator lines
    r"Ran \d+ stop hook",            # stop hook notifications
    r"Stop hook prevented",           # stop hook prevented
    r"✻ Brewed for",                 # brew time indicator
    r"✻ Churned for",                 # churn time indicator
    r"session:\d+m",                  # OMC session timer (changes every second)
    r"ctrl\+o to expand",            # expand hint
    r"shift\+tab to cycle",          # mode cycle hint
    r"Keel\s*$",                     # Keel mascot name
    r"\.-[oO]-[oOÒ]{2}-[oO]-\.",    # Keel face top
    r"\(_{4,}\)",                    # Keel face middle
    r"\|[°˚]\s+[°˚]\|",             # Keel eyes
    r"\|_{4}\|",                     # Keel face bottom
    r"^\s*⏵⏵\s",                     # permission mode prefix
    r"^\s*⏸\s",                      # plan mode prefix
]
_NOISE_RE = re.compile("|".join(f"(?:{p})" for p in NOISE_PATTERNS))


def _normalize_for_mirror_hash(text: str) -> str:
    """Strip volatile elements so hash is stable across TUI re-renders.
    Removes: timers (8m 49s), token counters (↑ 4.8k tokens), ctx %, numeric
    sequences in progress lines, and collapses whitespace."""
    # Timer patterns: "8m 49s", "1h 23m", "123s"
    text = re.sub(r"\b\d+[hms]\b\s*\d*[hms]?", "", text)
    # Token counters: "↑ 4.8k tokens", "↑ 123 tokens", "123k tokens"
    text = re.sub(r"[↑↓]\s*[\d.]+k?\s*tokens?", "", text, flags=re.IGNORECASE)
    # Context %: "12% context left", "context: 34%"
    text = re.sub(r"\d+%\s*(context|ctx)", "", text, flags=re.IGNORECASE)
    # Spinner chars at line start (already variant but double-safe)
    text = re.sub(r"^\s*[✻✶✽✢·●*◐◑◒◓⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*", "", text, flags=re.MULTILINE)
    # Any trailing numeric parentheses: "(8s)", "(1.2MB)"
    text = re.sub(r"\(\s*[\d.]+\s*\w*\s*\)", "", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_output(text: str) -> str:
    """Strip ANSI escapes, OSC sequences, status bar, and Claude Code TUI noise."""
    text = ANSI_RE.sub("", text)
    text = OSC_RE.sub("", text)
    text = text.replace("\xa0", " ")  # normalize non-breaking spaces
    lines = text.split("\n")
    lines = [l.rstrip() for l in lines if not _NOISE_RE.search(l)]
    # trim trailing empties
    while lines and not lines[-1].strip():
        lines.pop()
    # trim leading empties
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines)


def capture_pane(target: str, lines: int = SCROLLBACK_LINES) -> str:
    """Capture tmux pane content (used for prompt detection and /status)."""
    r = subprocess.run(
        ["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{lines}"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"tmux capture-pane failed: {r.stderr.strip()}")
    return r.stdout


def send_keys(target: str, text: str, literal: bool = True):
    """Send text to tmux pane via load-buffer + paste-buffer for safety.
    Handles special characters, quotes, newlines without crashing.
    Long text (>500 chars) is saved to a file and the path is sent instead."""
    if literal and len(text) > 500:
        # Long text: save to file and tell Claude to read it
        tmp = f"/tmp/liteclaw_input_{int(datetime.now().timestamp())}.txt"
        Path(tmp).write_text(text, encoding="utf-8")
        text = f"Read this file and follow the instructions inside: {tmp}"
        log.info(f"Long input saved to {tmp}")

    if not literal:
        # Non-literal: direct send-keys (for control sequences)
        subprocess.run(["tmux", "send-keys", "-t", target, text], check=True)
        return

    # Use load-buffer + paste-buffer to avoid send-keys special char issues
    tmp_buf = f"/tmp/liteclaw_buf_{os.getpid()}.txt"
    try:
        Path(tmp_buf).write_text(text, encoding="utf-8")
        subprocess.run(["tmux", "load-buffer", tmp_buf], check=True)
        subprocess.run(["tmux", "paste-buffer", "-t", target], check=True)
    finally:
        try:
            os.unlink(tmp_buf)
        except OSError:
            pass


def send_enter(target: str):
    """Send Enter key to tmux pane."""
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True)


def has_prompt(content: str) -> bool:
    """Check if a prompt indicator is visible in last lines."""
    last_lines = content.strip().split("\n")[-15:]
    for line in last_lines:
        # Replace nbsp with regular space before matching
        normalized = line.replace("\xa0", " ")
        if _PROMPT_RE.search(normalized):
            return True
    return False


def is_idle_prompt(content: str) -> bool:
    """Check if Claude Code is truly idle — prompt visible AND no activity indicators.
    More reliable than has_prompt() alone, which can trigger during tool call pauses."""
    lines = content.strip().split("\n")
    if not lines:
        return False
    # Check last 5 non-empty lines for activity indicators
    last_lines = [l for l in lines[-10:] if l.strip()]
    if not last_lines:
        return False
    # Must have prompt
    if not has_prompt(content):
        return False
    # Must NOT have any activity spinner in recent lines
    for line in last_lines[-5:]:
        if _ACTIVITY_PATTERNS.search(line):
            return False
    return True


# Patterns that indicate Claude Code is still working (tool calls in progress)
# Known Claude Code CLI activity labels (explicit list for documentation)
_ACTIVITY_LABELS = (
    "Doing|Reading|Running|Writing|Searching|Editing|Thinking|Calling|Executing"
    "|Computing|Channelling|Nesting|Brewing|Recalling|Initializing"
    "|Misting|Expanding|Parsing|Crafting|Focusing|Wondering|Pondering"
    "|Transfiguring"
)
# Spinner + capitalized word = activity. The spinner must sit at line start
# (after optional whitespace) — otherwise Claude Code's permanent UI chrome
# triggers false positives like "Opus 4.7 (1M context) · Claude Max" and
# "[✻] … · Share Claude Code…", which used to keep is_idle_prompt() returning
# False forever and hang cron jobs at the 120s busy-wait.
# Middle dot `·` and `*` are excluded because they are routine separators, not
# actual spinner frames used by Claude Code.
_SPINNER_CHARS = r"[✻✶✽✢●◐◑◒◓⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]"
_ACTIVITY_PATTERNS = re.compile(
    rf"^\s*{_SPINNER_CHARS}\s+[A-Z][a-z]{{2,}}"   # line-anchored: ✢ Transfiguring
    r"|\(thinking\)"
)


def detect_interactive_prompt(content: str) -> dict | None:
    """Detect if Claude Code is showing an interactive selection prompt.
    Returns dict with 'question' and 'options' if found, else None.

    Claude Code interactive prompts look like:
      ? Which option do you prefer?
      ❯ Option A (selected)
        Option B
        Option C
    Or AskUserQuestion with numbered options:
      ? Question text
        1. Option one
        2. Option two
    """
    lines = content.strip().split("\n")
    if not lines:
        return None

    # Find question line (starts with ? or contains a question waiting for input)
    question = None
    question_idx = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Claude Code question prompt: "? question text"
        if stripped.startswith("? ") and len(stripped) > 5:
            question = stripped[2:]
            question_idx = i
        # Also match "Select:" or similar
        elif re.match(r"^(Select|Choose|Pick)\s", stripped, re.IGNORECASE):
            question = stripped
            question_idx = i

    if question is None or question_idx < 0:
        return None

    # Collect options after the question
    options = []
    for line in lines[question_idx + 1:]:
        stripped = line.strip()
        if not stripped:
            continue
        # Option patterns: "❯ Option" / "  Option" / "1. Option" / "• Option"
        m = re.match(r"^[❯►>•]\s+(.+)", stripped)
        if m:
            options.append(m.group(1).strip())
            continue
        m = re.match(r"^\d+[.)]\s+(.+)", stripped)
        if m:
            options.append(m.group(1).strip())
            continue
        m = re.match(r"^\s{2,}(.+)", line)  # indented = option
        if m and not _PROMPT_RE.search(stripped) and not _ACTIVITY_PATTERNS.search(stripped):
            options.append(m.group(1).strip())
            continue
        # Stop at prompt or non-option content
        if _PROMPT_RE.search(stripped):
            break

    if len(options) >= 2:
        return {"question": question, "options": options[:10]}  # max 10 options
    return None


def _detect_yn_prompt(content: str) -> str | None:
    """Detect Y/N style confirmation prompts.
    Returns:
      "Y" if [Y/n] (default Yes),
      "N" if [y/N] (default No),
      "?" if "Do you want to proceed",
      None otherwise.
    Only looks at the last 10 non-empty lines to avoid stale prompts.
    """
    lines = [l for l in content.strip().split("\n") if l.strip()][-10:]
    joined = "\n".join(lines)
    if re.search(r"\[Y/n\]\s*$", joined, re.MULTILINE):
        return "Y"
    if re.search(r"\[y/N\]\s*$", joined, re.MULTILINE):
        return "N"
    if "Do you want to proceed" in joined:
        return "?"
    return None


# Phase D: reasoning-block detection patterns
_REASONING_LINE_RE = re.compile(
    r"^\s*[✻✶✽✢·●\*◐◑◒◓⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*"
    r"(Thinking|Pondering|Wondering|thinking)",
    re.IGNORECASE,
)
_REASONING_INLINE_RE = re.compile(r"\(thinking\)", re.IGNORECASE)


def _split_reasoning(text: str) -> tuple[str, str]:
    """Phase D: Split text into (reasoning, answer).

    Reasoning is the collection of Claude Code "Thinking..." / "(thinking)" blocks
    (often indented/quoted continuation lines). If no reasoning is detected, returns
    ("", text) unchanged.

    Safety: if the split would leave an empty answer (everything looked like reasoning),
    falls back to the original text as the answer and empty reasoning.
    """
    if not text:
        return "", text
    lines = text.split("\n")
    reasoning_lines: list[str] = []
    answer_lines: list[str] = []
    in_thinking_block = False
    for line in lines:
        if _REASONING_LINE_RE.match(line) or _REASONING_INLINE_RE.search(line):
            reasoning_lines.append(line)
            in_thinking_block = True
            continue
        if in_thinking_block and line.strip().startswith(("> ", "│ ", "┃ ")):
            # continuation of thinking block (indented/quoted)
            reasoning_lines.append(line)
            continue
        in_thinking_block = False
        answer_lines.append(line)
    reasoning = "\n".join(reasoning_lines).strip()
    answer = "\n".join(answer_lines).strip()
    # Safety: if reasoning ate everything, fallback to whole text as answer
    if not answer and reasoning:
        return "", text
    return reasoning, answer or text


def get_pane_cwd(target: str) -> str:
    """Get the current working directory of the tmux pane."""
    r = subprocess.run(
        ["tmux", "display-message", "-t", target, "-p", "#{pane_current_path}"],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else os.getcwd()


def split_message(text: str, max_len: int = TG_MAX_LEN) -> list[str]:
    """Split text into chunks respecting line boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    current = []
    current_len = 0

    for line in text.split("\n"):
        line_len = len(line) + 1
        if current_len + line_len > max_len and current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len

    if current:
        chunks.append("\n".join(current))
    return chunks


def format_for_telegram(text: str) -> str:
    """Format raw CLI output as Telegram HTML for readability."""
    lines = text.split("\n")
    result = []
    in_code_block = False

    for line in lines:
        if line.strip().startswith("```"):
            if in_code_block:
                result.append("</code></pre>")
                in_code_block = False
            else:
                lang = line.strip().removeprefix("```").strip()
                result.append(f"<pre><code class=\"language-{lang}\">" if lang else "<pre><code>")
                in_code_block = True
            continue

        if in_code_block:
            result.append(html.escape(line))
        else:
            escaped = html.escape(line)
            escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
            escaped = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escaped)
            result.append(escaped)

    if in_code_block:
        result.append("</code></pre>")

    return "\n".join(result)


# =============================================================================
# Summarizer
# =============================================================================

SUMMARIZE_PROMPT = """You are a REFORMATTER. Your ONLY job is to clean up raw terminal output for Telegram.

CRITICAL RULES:
- You are NOT the assistant that produced this output. Do NOT respond to the user's question.
- Do NOT say "I can't do X" or "I don't have access to Y" — you are reformatting, not answering.
- Do NOT add your own opinions, suggestions, or commentary.
- If the output shows work-in-progress (tool calls, file edits), summarize WHAT WAS DONE, not what you think about it.
- If the output contains errors from the CLI, report them as-is — do NOT apologize for them.
- JUST REFORMAT. Nothing else.

Reformatting rules:
- Extract the meaningful response, discard terminal noise (ANSI codes, hook messages, status bars)
- Keep code blocks, commands, key decisions, and action items intact
- Respond in the same language as the user's question
- If the output contains an error, highlight it clearly
- Keep it concise but NEVER drop important content — completeness over brevity

PRESERVE-AS-IS (never compress or drop):
- Numbered or lettered option lists presented for the user to choose from.
  Examples:
    "1. …  2. …  3. …"
    "A) …  B) …  C) …"
    "**B** — …  **C** — …  **D** — …"
  If the original asks "B/C/D 중 뭘로?", "which one?", "번호 골라", "pick one",
  "선택해줘", "choose", etc., keep EVERY option line verbatim. Dropping the
  option descriptions makes the message unanswerable via Telegram.
- Explicit questions directed at the user ("...할까요?", "which should I …?",
  "가? 아니면 …?"). Keep them intact.
- Any line that starts with a choice marker ("[A]", "(B)", "1)", "- A:", etc.)
  within a section that is clearly a choice menu.

Telegram formatting rules:
- NO markdown tables (|---|) — use bullet points: "항목: 설명" or "항목 → 설명"
- NO # headers — use **bold text** for section titles
- Use bullet points (•, -, ◦) for lists
- Use `code` for inline code, triple backticks for code blocks
- Use **bold** for emphasis, not ALL CAPS
- Keep lines short — long lines wrap badly on mobile
- Separate sections with blank lines, not horizontal rules"""




# =============================================================================
# Cron helper (Unix-standard day-of-week semantics)
# =============================================================================

# APScheduler 3.x CronTrigger.from_crontab() treats numeric day-of-week
# with APScheduler's internal index (0=Mon, 6=Sun) rather than Unix cron
# (0/7=Sun, 1=Mon, ..., 6=Sat). Result: "1-5" means Tue-Sat, not Mon-Fri.
# Observed on 2026-04-18 (Sat) when weekday-only crons fired.
_DOW_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


def _unix_dow_to_name(tok: str) -> str:
    return _DOW_NAMES[int(tok) % 7]


def _translate_dow_part(part: str) -> str:
    part = part.strip()
    if not part or part == "*" or any(c.isalpha() for c in part):
        return part
    if "/" in part:
        base, step = part.split("/", 1)
        return f"{_translate_dow_part(base)}/{step}"
    if "-" in part:
        a, b = part.split("-", 1)
        return f"{_unix_dow_to_name(a)}-{_unix_dow_to_name(b)}"
    return _unix_dow_to_name(part)


def build_cron_trigger(cron_expr: str, tz):
    """Build APScheduler CronTrigger with Unix-standard weekday indexing."""
    from apscheduler.triggers.cron import CronTrigger
    parts = cron_expr.split()
    if len(parts) != 5:
        raise ValueError(f"Cron expression must have 5 fields: {cron_expr!r}")
    minute, hour, day, month, dow = parts
    dow_translated = ",".join(_translate_dow_part(p) for p in dow.split(","))
    return CronTrigger(
        minute=minute, hour=hour, day=day, month=month,
        day_of_week=dow_translated, timezone=tz,
    )


# =============================================================================
# Dashboard
# =============================================================================

class DashboardHandler(BaseHTTPRequestHandler):
    """Minimal HTTP dashboard for LiteClaw settings."""

    def __init__(self, bridge, *args, **kwargs):
        self.bridge = bridge
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        pass  # suppress default logging

    def _send_json(self, data, status=200):
        body = _json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_content):
        body = html_content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/config":
            self._send_json({
                "summarizer_model": SUMMARIZER_MODEL,
                "tmux_target": self.bridge.target,
                "raw_mode": self.bridge.raw_mode,
                "scrollback_lines": SCROLLBACK_LINES,
                "poll_interval": POLL_INTERVAL,
                "dashboard_port": DASHBOARD_PORT,
            })
        elif self.path == "/api/status":
            self._send_json({
                "busy": self.bridge.busy,
                "target": self.bridge.target,
                "raw_mode": self.bridge.raw_mode,
                "api_available": self.bridge._api_available,
                "pipe_active": self.bridge._pipe_active,
                "last_activity": getattr(self.bridge, '_last_activity', None),
            })
        elif self.path == "/api/logs":
            try:
                log_path = self.bridge._get_log_path() if self.bridge._pipe_active else ""
                recent = []
                if log_path and os.path.exists(log_path):
                    with open(log_path, "r", errors="replace") as f:
                        lines = f.readlines()
                        recent = [l.rstrip() for l in lines[-20:]]
                self._send_json({"lines": recent})
            except Exception as e:
                self._send_json({"lines": [], "error": str(e)})
        elif self.path == "/api/evolve":
            evolve_dir = Path.home() / "projects/liteclaw/.evolve/data"
            ideas_count = proposals_count = pending_proposals = rules_count = 0
            if evolve_dir.exists():
                ideas_file = evolve_dir / "ideas.jsonl"
                proposals_file = evolve_dir / "proposals.jsonl"
                rejections_file = evolve_dir / "rejections.jsonl"
                if ideas_file.exists():
                    ideas_count = sum(1 for _ in ideas_file.open())
                if proposals_file.exists():
                    for line in proposals_file.open():
                        proposals_count += 1
                        try:
                            p = _json.loads(line)
                            if p.get("decision") == "pending":
                                pending_proposals += 1
                        except Exception:
                            pass
                if rejections_file.exists():
                    rules_count = sum(1 for _ in rejections_file.open())
            skills = {k: v for k, v in self.bridge._skills.items()} if hasattr(self.bridge, '_skills') else {}
            self._send_json({
                "ideas": ideas_count,
                "proposals": proposals_count,
                "pending_proposals": pending_proposals,
                "rejection_rules": rules_count,
                "skills": skills,
            })
        elif self.path == "/":
            self._send_html(DASHBOARD_HTML)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/config":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = _json.loads(self.rfile.read(length)) if length else {}
                global SUMMARIZER_MODEL
                if "summarizer_model" in body:
                    SUMMARIZER_MODEL = body["summarizer_model"]
                if "raw_mode" in body:
                    self.bridge.raw_mode = bool(body["raw_mode"])
                if "tmux_target" in body:
                    old = self.bridge.target
                    self.bridge.target = body["tmux_target"]
                    if old != self.bridge.target and self.bridge._pipe_active:
                        self.bridge._stop_pipe()
                        self.bridge._start_pipe()
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, status=400)
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LiteClaw Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; padding: 20px; max-width: 640px; margin: 0 auto; }
  h1 { font-size: 1.5rem; margin-bottom: 20px; color: #38bdf8; }
  .card { background: #1e293b; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
  .card h2 { font-size: 0.9rem; color: #94a3b8; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 1px; }
  .row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid #334155; }
  .row:last-child { border-bottom: none; }
  .label { color: #94a3b8; font-size: 0.85rem; }
  .value { color: #f1f5f9; font-weight: 500; }
  select, input[type=text] { background: #334155; color: #f1f5f9; border: 1px solid #475569; border-radius: 4px; padding: 6px 10px; font-size: 0.85rem; }
  button { background: #2563eb; color: white; border: none; border-radius: 4px; padding: 8px 16px; cursor: pointer; font-size: 0.85rem; }
  button:hover { background: #1d4ed8; }
  .toggle { position: relative; width: 44px; height: 24px; background: #475569; border-radius: 12px; cursor: pointer; transition: background 0.2s; }
  .toggle.on { background: #22c55e; }
  .toggle::after { content: ''; position: absolute; top: 2px; left: 2px; width: 20px; height: 20px; background: white; border-radius: 50%; transition: left 0.2s; }
  .toggle.on::after { left: 22px; }
  .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .status-dot.idle { background: #22c55e; }
  .status-dot.busy { background: #f59e0b; animation: pulse 1s infinite; }
  @keyframes pulse { 50% { opacity: 0.5; } }
  .logs { background: #0f172a; border-radius: 4px; padding: 10px; font-family: monospace; font-size: 0.75rem; color: #94a3b8; max-height: 200px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }
  .footer { text-align: center; color: #475569; font-size: 0.75rem; margin-top: 20px; }
</style>
</head>
<body>
<h1>LiteClaw Dashboard</h1>

<div class="card">
  <h2>Status</h2>
  <div class="row"><span class="label">State</span><span class="value" id="state"><span class="status-dot idle"></span>Loading...</span></div>
  <div class="row"><span class="label">Target</span><span class="value" id="target">-</span></div>
  <div class="row"><span class="label">API Proxy</span><span class="value" id="api">-</span></div>
</div>

<div class="card">
  <h2>Settings</h2>
  <div class="row">
    <span class="label">Summarizer Model</span>
    <select id="model" onchange="saveConfig()">
      <option value="claude-haiku-4-5">Haiku</option>
      <option value="claude-sonnet-4-6">Sonnet</option>
      <option value="claude-opus-4-6">Opus</option>
    </select>
  </div>
  <div class="row">
    <span class="label">Raw Mode</span>
    <div id="rawToggle" class="toggle" onclick="toggleRaw()"></div>
  </div>
  <div class="row">
    <span class="label">tmux Target</span>
    <input type="text" id="targetInput" style="width:120px" onchange="saveConfig()">
  </div>
</div>

<div class="card">
  <h2>Recent Logs</h2>
  <div class="logs" id="logs">Loading...</div>
</div>

<div class="footer">LiteClaw Dashboard &middot; Port <span id="port">7777</span></div>

<script>
async function load() {
  try {
    const [cfg, st, lg] = await Promise.all([
      fetch('/api/config').then(r=>r.json()),
      fetch('/api/status').then(r=>r.json()),
      fetch('/api/logs').then(r=>r.json()),
    ]);
    document.getElementById('model').value = cfg.summarizer_model;
    document.getElementById('targetInput').value = cfg.tmux_target;
    document.getElementById('port').textContent = cfg.dashboard_port;
    const rawEl = document.getElementById('rawToggle');
    if (cfg.raw_mode) rawEl.classList.add('on'); else rawEl.classList.remove('on');
    const dot = st.busy ? '<span class="status-dot busy"></span>Working...' : '<span class="status-dot idle"></span>Idle';
    document.getElementById('state').innerHTML = dot;
    document.getElementById('target').textContent = st.target;
    document.getElementById('api').textContent = st.api_available ? 'Connected' : 'Unavailable';
    document.getElementById('logs').textContent = (lg.lines || []).join('\\n') || 'No logs';
  } catch(e) { console.error(e); }
}
async function saveConfig() {
  await fetch('/api/config', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      summarizer_model: document.getElementById('model').value,
      tmux_target: document.getElementById('targetInput').value,
    })
  });
  load();
}
async function toggleRaw() {
  const el = document.getElementById('rawToggle');
  const newVal = !el.classList.contains('on');
  await fetch('/api/config', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ raw_mode: newVal })
  });
  load();
}
load();
setInterval(load, 5000);
</script>
</body>
</html>"""


# =============================================================================
# LiteClaw
# =============================================================================

class LiteClaw:
    def __init__(self):
        self.target = TMUX_TARGET
        self.busy = False
        self.raw_mode = False
        self._pipe_active = False
        self._log_path = ""
        self._log_offset = 0
        self._summarizer_ready = False  # Tier 2 tmux agent is running
        self._api_available = None      # None=unknown, True/False after probe
        self._last_activity = None
        self._followup_task: asyncio.Task | None = None  # track active follow-up
        # Multi-agent registry: {name: {session, project, status}}
        self._agents: dict[str, dict] = {}
        self._agents_file = Path(__file__).parent / ".agents.json"
        self._load_agents()
        # Skill loader registry
        self._skills: dict[str, dict] = {}
        # v0.5.0 UX Overhaul state
        # F1 Mirror
        self.mirror_on = MIRROR_ENABLED
        self._mirror_task: asyncio.Task | None = None
        self._last_mirror_hash = ""
        self._mirror_paused_until = 0.0  # time.monotonic()
        self._last_mirror_capture = ""
        self._bot_ref = None  # populated in run() post-init
        # F2 Draft
        self.draft_on = DRAFT_STREAM_ENABLED
        self._draft_msg_id: int | None = None
        self._draft_last_hash = ""
        self._draft_last_edit_at = 0.0
        self._draft_interval = DRAFT_STREAM_INTERVAL
        # F3 Reasoning
        self.reasoning_on = REASONING_LANE_ENABLED
        self._reasoning_msg_id: int | None = None
        # F4 Interactive
        self._last_interactive_options: list[str] = []  # for free-form parser
        # F6 Skills home
        LITECLAW_HOME.mkdir(parents=True, exist_ok=True)
        SKILLS_PATH.mkdir(parents=True, exist_ok=True)
        # Load persisted toggles (override env defaults if present)
        self._load_config()
        # Evolve proposal notification tracking
        self._notified_proposals: set[str] = set()
        # Cron scheduler
        self._cron_jobs: list[dict] = []
        self._cron_file = Path(__file__).resolve().parent / ".cron_jobs.json"
        self._cron_running: set[str] = set()  # job ids currently executing
        self._interactive_sent = False  # prevent duplicate interactive prompts
        self._load_cron_jobs()
        # Session recovery state file
        self._state_file = Path(__file__).resolve().parent / ".liteclaw_state.json"
        # Cached LiteClaw-owned session UUID (for tagging transcript entries
        # and scoping /recall session). Refreshed at startup and whenever
        # sessions.json changes. `None` if no id allocated yet.
        self._current_session_id: str | None = self._load_current_session_id()
        # Phase A: jsonl-based response extraction state.
        self._jsonl_offset: int = 0
        self._skip_summarizer_once: bool = False

    def _load_config(self):
        """Load persisted runtime toggles from ~/.liteclaw/config.json."""
        try:
            if CONFIG_PATH.exists():
                data = _json.loads(CONFIG_PATH.read_text())
                # Only override toggles, not env-controlled tuning
                self.mirror_on = bool(data.get("mirror_on", self.mirror_on))
                self.reasoning_on = bool(data.get("reasoning_on", self.reasoning_on))
                self.draft_on = bool(data.get("draft_on", self.draft_on))
                self.raw_mode = bool(data.get("raw_mode", self.raw_mode))
                log.info(f"Loaded runtime config from {CONFIG_PATH}")
        except Exception as e:
            log.warning(f"Failed to load config: {e}")

    def _save_config(self):
        """Persist runtime toggles to ~/.liteclaw/config.json."""
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "mirror_on": self.mirror_on,
                "reasoning_on": self.reasoning_on,
                "draft_on": self.draft_on,
                "raw_mode": self.raw_mode,
            }
            CONFIG_PATH.write_text(_json.dumps(data, indent=2))
        except Exception as e:
            log.warning(f"Failed to save config: {e}")

    async def _edit_with_retry(self, bot, chat_id: int, msg_id: int, text: str,
                               parse_mode: str | None = None, max_attempts: int = 3) -> bool:
        """Edit a Telegram message with 3x backoff. Returns True on success.
        Silently absorbs 'not modified' and 'message to edit not found' errors.
        On 429 (rate limit), uses exponential backoff."""
        if not msg_id:
            return False
        backoff = 0.5
        for attempt in range(max_attempts):
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id, text=text,
                    parse_mode=parse_mode,
                )
                return True
            except Exception as e:
                msg = str(e).lower()
                if "not modified" in msg or "message to edit not found" in msg:
                    return True  # benign
                if "too many requests" in msg or "flood" in msg:
                    # Extract retry_after if present, else use escalating backoff
                    import re as _re
                    m = _re.search(r"retry after (\d+)", msg)
                    wait = int(m.group(1)) if m else int(backoff * 4)
                    log.warning(f"Telegram rate-limited, waiting {wait}s")
                    await asyncio.sleep(wait)
                    backoff = min(backoff * 2, 10)
                    continue
                if attempt < max_attempts - 1:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                else:
                    log.warning(f"edit_message_text failed after {max_attempts} attempts: {e}")
                    return False
        return False

    def _load_agents(self):
        """Load agent registry from .agents.json."""
        if self._agents_file.exists():
            try:
                data = _json.loads(self._agents_file.read_text())
                self._agents = data
                log.info(f"Loaded {len(self._agents)} agent(s) from {self._agents_file}")
            except Exception as e:
                log.warning(f"Failed to load agents file: {e}")
                self._agents = {}
        # Reconcile: check which agent sessions are still alive
        for name, info in list(self._agents.items()):
            r = subprocess.run(
                ["tmux", "has-session", "-t", info["session"]],
                capture_output=True,
            )
            if r.returncode != 0:
                log.info(f"Agent '{name}' session '{info['session']}' no longer exists, marking dead")
                info["status"] = "dead"

    def _save_agents(self):
        """Persist agent registry to .agents.json."""
        try:
            self._agents_file.write_text(_json.dumps(self._agents, indent=2))
        except Exception as e:
            log.warning(f"Failed to save agents file: {e}")

    def _save_state(self):
        """Save pane snapshot hash for session recovery."""
        try:
            pane = capture_pane(self.target, lines=SCROLLBACK_LINES)
            state = {
                "last_pane_hash": hashlib.md5(pane.encode()).hexdigest(),
                "last_pane_tail": pane.strip().split("\n")[-20:],  # last 20 lines
                "timestamp": datetime.now().isoformat(),
                "target": self.target,
            }
            self._state_file.write_text(_json.dumps(state, indent=2, ensure_ascii=False))
        except Exception as e:
            log.warning(f"Failed to save state: {e}")

    async def _recover_pending_messages(self, bot):
        """Check if Claude responded after LiteClaw's last shutdown and deliver missed messages."""
        if not self._state_file.exists():
            log.info("No previous state — skipping session recovery")
            return

        try:
            state = _json.loads(self._state_file.read_text())
        except Exception:
            return

        saved_target = state.get("target", "")
        if saved_target != self.target:
            log.info(f"Target changed ({saved_target} → {self.target}), skipping recovery")
            return

        saved_hash = state.get("last_pane_hash", "")
        saved_tail = state.get("last_pane_tail", [])

        try:
            current_pane = capture_pane(self.target, lines=SCROLLBACK_LINES)
        except RuntimeError:
            return

        current_hash = hashlib.md5(current_pane.encode()).hexdigest()
        if current_hash == saved_hash:
            log.info("Pane unchanged since last session — no recovery needed")
            return

        # Find new content by diffing against saved tail
        current_lines = current_pane.strip().split("\n")
        saved_tail_str = "\n".join(saved_tail).strip()

        # Find where saved tail ends in current pane
        new_content = ""
        for i in range(len(current_lines)):
            window = "\n".join(current_lines[i:i+len(saved_tail)]).strip()
            if window == saved_tail_str:
                # Everything after this match is new
                remaining = current_lines[i+len(saved_tail):]
                new_content = clean_output("\n".join(remaining)).strip()
                break

        if not new_content or len(new_content) < 30:
            log.info("No significant new content since last session")
            return

        # Check if it's just a prompt (no actual response)
        if all(line.strip() == "" or "❯" in line for line in new_content.split("\n")):
            return

        log.info(f"Session recovery: found {len(new_content)} chars of undelivered content")

        # Summarize and deliver
        try:
            if self._api_available and len(new_content) > 50:
                summary = await asyncio.wait_for(
                    self._summarize("(session recovery)", new_content),
                    timeout=30.0,
                )
                new_content = summary
        except Exception:
            pass  # deliver raw if summarize fails

        header = "📋 이전 세션 미전달 메시지:\n\n"
        for chunk in split_message(header + new_content):
            try:
                await bot.send_message(chat_id=CHAT_ID, text=chunk)
            except Exception as e:
                log.warning(f"Recovery delivery failed: {e}")

        self._save_state()
        log.info("Session recovery complete")

    def _load_skills(self):
        """Load skills from ~/.liteclaw/skills/ (supports .py and .md).

        Migrates legacy ~/.liteclaw-evolve/skills/ contents on first run when
        the new skills path is empty.
        """
        # Migrate legacy path (copy only when new dir empty)
        legacy = Path.home() / ".liteclaw-evolve" / "skills"
        try:
            if legacy.exists() and SKILLS_PATH.exists() and not any(SKILLS_PATH.iterdir()):
                import shutil
                for f in legacy.iterdir():
                    if f.is_file():
                        shutil.copy2(f, SKILLS_PATH / f.name)
                log.info(f"Migrated skills from {legacy} -> {SKILLS_PATH}")
        except Exception as e:
            log.warning(f"Skill migration failed: {e}")

        if not SKILLS_PATH.exists():
            return

        # Remove previously registered skill handlers before reload (so removed
        # skills don't linger and reloads don't duplicate handlers).
        self._unregister_skill_handlers()

        self._skills = {}
        _SKILL_SKIP_NAMES = {"README.md", "README_KO.md", "README.ko.md"}
        for skill_file in sorted(SKILLS_PATH.iterdir()):
            if skill_file.name.startswith("_") or skill_file.name.startswith("."):
                continue
            if not skill_file.is_file():
                continue
            if skill_file.name in _SKILL_SKIP_NAMES:
                continue
            if skill_file.suffix == ".py":
                self._load_skill_py(skill_file)
            elif skill_file.suffix == ".md":
                self._load_skill_md(skill_file)

    def _unregister_skill_handlers(self):
        """Remove previously-registered skill CommandHandlers from the app."""
        if not getattr(self, "_app", None) or not self._skills:
            return
        try:
            for group_id, group_handlers in list(self._app.handlers.items()):
                for h in list(group_handlers):
                    if isinstance(h, CommandHandler):
                        cmds = getattr(h, "commands", frozenset()) or frozenset()
                        if any(c in self._skills for c in cmds):
                            try:
                                self._app.remove_handler(h, group=group_id)
                            except Exception:
                                pass
        except Exception as e:
            log.warning(f"Failed to unregister old skill handlers: {e}")

    def _load_skill_py(self, skill_file: Path):
        """Load a Python skill file. Requires whitelist approval."""
        whitelist_path = SKILLS_PATH / "_whitelist.json"
        approved: list = []
        if whitelist_path.exists():
            try:
                approved = _json.loads(whitelist_path.read_text())
            except Exception:
                log.warning(f"Could not read whitelist {whitelist_path}")
                return
        if skill_file.name not in approved:
            log.info(f"Skill '{skill_file.name}' not in whitelist, skipping")
            return
        try:
            ns = {
                "capture_pane": capture_pane, "send_keys": send_keys,
                "send_enter": send_enter, "clean_output": clean_output,
                "log": log, "Path": Path, "subprocess": subprocess,
                "_json": _json, "asyncio": asyncio, "httpx": httpx,
            }
            exec(compile(skill_file.read_text(), str(skill_file), "exec"), ns)
            cmd_name = ns.get("COMMAND")
            handler_fn = ns.get("handler")
            if not (cmd_name and handler_fn):
                return

            async def _wrapped(update, ctx, _fn=handler_fn, _claw=self):
                if not _claw._auth(update):
                    return
                await _fn(_claw, update, ctx)

            if self._app:
                self._app.add_handler(CommandHandler(cmd_name, _wrapped))
            self._skills[cmd_name] = {
                "file": skill_file.name,
                "type": "py",
                "desc": ns.get("DESCRIPTION", ""),
            }
            log.info(f"Loaded PY skill: /{cmd_name} from {skill_file.name}")
        except Exception as e:
            log.warning(f"Failed to load PY skill {skill_file.name}: {e}")

    def _load_skill_md(self, path: Path):
        """Load a Markdown skill: YAML frontmatter + prompt template."""
        try:
            content = path.read_text(encoding="utf-8")
            if not content.startswith("---"):
                log.warning(f"Skill {path.name}: missing YAML frontmatter")
                return
            parts = content.split("---", 2)
            if len(parts) < 3:
                return
            # Parse frontmatter. Prefer pyyaml; fall back to minimal parser.
            try:
                import yaml
                meta = yaml.safe_load(parts[1]) or {}
            except Exception:
                meta = {}
                for line in parts[1].splitlines():
                    if ":" not in line:
                        continue
                    k, _, v = line.partition(":")
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k:
                        meta[k] = v
            prompt_template = parts[2].strip()
            cmd_name = meta.get("command") or path.stem
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", str(cmd_name)):
                log.warning(f"Skill {path.name}: invalid command name '{cmd_name}'")
                return
            description = meta.get("description", "") or ""

            async def md_handler(update, context, _template=prompt_template, _name=cmd_name):
                if not self._auth(update):
                    return
                args_text = " ".join(context.args) if context.args else ""
                rendered = _template.replace("{{args}}", args_text)
                await self._inject_prompt_from_skill(update, context, rendered, _name)

            if self._app:
                self._app.add_handler(CommandHandler(cmd_name, md_handler))
            self._skills[cmd_name] = {
                "file": path.name,
                "type": "md",
                "desc": description,
            }
            log.info(f"Loaded MD skill: /{cmd_name} from {path.name}")
        except Exception as e:
            log.warning(f"Failed to load MD skill {path.name}: {e}")

    async def _inject_prompt_from_skill(self, update, context, rendered_prompt: str, skill_name: str):
        """Inject a rendered skill prompt into the Claude session as if the
        user had typed it. Reuses the main handle_message pipeline."""
        if self.busy:
            await update.message.reply_text("⏳ Claude is busy, try again shortly.")
            return
        await update.message.reply_text(f"▶️ Running skill: /{skill_name}")
        try:
            update.message.text = rendered_prompt
        except Exception:
            try:
                object.__setattr__(update.message, "text", rendered_prompt)
            except Exception as e:
                log.warning(f"Skill /{skill_name}: cannot rewrite text ({e})")
                await update.message.reply_text("❌ Cannot inject skill prompt.")
                return
        await self.handle_message(update, context)

    async def _register_native_commands(self, bot=None):
        """Register all loaded commands with Telegram's native menu
        (overrides OpenClaw pollution)."""
        if not SKILLS_NATIVE_MENU:
            return
        try:
            b = bot or self._bot_ref
            if not b:
                return
            from telegram import BotCommand
            cmds = [
                BotCommand("start", "Show help"),
                BotCommand("help", "Show help"),
                BotCommand("status", "Show last 30 lines of pane"),
                BotCommand("raw", "Toggle raw output mode"),
                BotCommand("mirror", "Toggle CLI→Telegram mirror"),
                BotCommand("reasoning", "Toggle thinking lane"),
                BotCommand("lcskill", "Manage liteclaw skills"),
                BotCommand("cancel", "Ctrl+C current pane"),
                BotCommand("escape", "Send Escape"),
                BotCommand("sessions", "List tmux sessions"),
                BotCommand("agents", "List agents"),
                BotCommand("recall", "Search history"),
                BotCommand("cron", "Manage cron jobs"),
            ]
            for name, info in self._skills.items():
                desc = info.get("desc", "") or "Skill"
                cmds.append(BotCommand(name, desc[:256]))
            await b.set_my_commands(cmds)
            log.info(f"Registered {len(cmds)} Telegram native commands")
        except Exception as e:
            log.warning(f"Native command register failed: {e}")

    async def _native_menu_periodic(self):
        """Periodically re-register native commands to resist OpenClaw
        gateway restarts that overwrite setMyCommands."""
        while True:
            await asyncio.sleep(600)  # 10 min
            try:
                await self._register_native_commands()
            except Exception:
                pass

    async def _skills_hot_reload_loop(self):
        """Watch SKILLS_PATH for mtime changes and reload on change."""
        if not SKILLS_HOT_RELOAD:
            return
        last_sig = None
        while True:
            try:
                await asyncio.sleep(10)
                if not SKILLS_PATH.exists():
                    continue
                sig = 0
                for f in SKILLS_PATH.iterdir():
                    if f.is_file() and not f.name.startswith("."):
                        try:
                            sig ^= hash((f.name, int(f.stat().st_mtime)))
                        except Exception:
                            pass
                if last_sig is not None and sig != last_sig:
                    log.info("Skill directory changed, hot-reloading...")
                    self._load_skills()
                    if self._bot_ref:
                        await self._register_native_commands()
                last_sig = sig
            except Exception as e:
                log.warning(f"Skills hot-reload loop error: {e}")

    # -- Cron job management --

    def _load_cron_jobs(self):
        """Load cron jobs from .cron_jobs.json."""
        if self._cron_file.exists():
            try:
                self._cron_jobs = _json.loads(self._cron_file.read_text())
                log.info(f"Loaded {len(self._cron_jobs)} cron job(s)")
            except Exception as e:
                log.warning(f"Failed to load cron jobs: {e}")
                self._cron_jobs = []

    def _save_cron_jobs(self):
        """Persist cron jobs to .cron_jobs.json."""
        try:
            self._cron_file.write_text(_json.dumps(self._cron_jobs, indent=2))
        except Exception as e:
            log.warning(f"Failed to save cron jobs: {e}")

    def _get_cron_job(self, job_id: str) -> dict | None:
        """Find a cron job by id."""
        for job in self._cron_jobs:
            if job["id"] == job_id:
                return job
        return None

    def _schedule_cron_jobs(self, job_queue):
        """Register all enabled cron jobs with the bot's JobQueue."""
        for job in self._cron_jobs:
            if not job.get("enabled", True):
                continue
            try:
                tz = job.get("tz", "Asia/Seoul")
                trigger = build_cron_trigger(job["cron_expr"], tz)
                job_queue.run_custom(
                    callback=self._run_cron_job,
                    job_kwargs={
                        "trigger": trigger,
                        "id": f"cron-{job['id']}",
                        "replace_existing": True,
                    },
                    data=job,
                )
                log.info(f"Cron job '{job['id']}' scheduled: {job['cron_expr']} ({tz})")
            except Exception as e:
                log.warning(f"Failed to schedule cron job '{job['id']}': {e}")

    async def _run_cron_job(self, context):
        """Execute a single cron job. Called by APScheduler."""
        job = context.job.data
        job_id = job["id"]
        session_name = f"cron-{job_id}"
        bot = context.bot

        # Overlap prevention
        if job_id in self._cron_running:
            log.info(f"Cron '{job_id}' already running, skipping")
            return
        self._cron_running.add(job_id)

        now = datetime.now(ZoneInfo(job.get("tz", "Asia/Seoul")))
        log.info(f"Cron '{job_id}' starting at {now.strftime('%H:%M:%S')}")

        try:
            # Ensure tmux session exists
            if not self._agent_session_alive(session_name):
                project = job.get("project", "~")
                subprocess.run(
                    ["tmux", "new-session", "-d", "-s", session_name,
                     "-x", "200", "-y", "50"],
                    check=True, capture_output=True,
                )
                subprocess.run(
                    ["tmux", "send-keys", "-t", session_name,
                     f"cd {project} && claude --dangerously-skip-permissions", "Enter"],
                    check=True,
                )
                # Wait for Claude Code prompt, auto-accepting the trust dialog
                # that Claude shows on first launch in a new cwd. Without this,
                # crons that run in unattended project dirs hang on "Do you
                # trust the files in this folder?" and time out at 120s.
                trust_accepted = False
                for _ in range(45):
                    await asyncio.sleep(1)
                    try:
                        content = capture_pane(session_name, lines=20)
                    except RuntimeError:
                        continue
                    if not trust_accepted and re.search(
                        r"Yes, I trust|Do you trust the files|Trust the files in this folder",
                        content,
                    ):
                        log.info(f"Cron '{job_id}': trust prompt detected, sending Enter")
                        subprocess.run(["tmux", "send-keys", "-t", session_name, "Enter"], check=False)
                        trust_accepted = True
                        await asyncio.sleep(2)
                        continue
                    if has_prompt(content):
                        break
                else:
                    raise TimeoutError("Claude Code prompt not detected after 45s")

            # Wait for the cron's claude session to settle before sending.
            # Prefer is_idle_prompt (strict: prompt + no spinner) but fall back
            # to has_prompt + stable pane content when the banner / proxy load
            # keeps a faint spinner lingering. Without the fallback cron jobs
            # that launch Claude Code fresh on a busy host fail with
            # "Session busy for 120s" even though the pane is obviously ready.
            idle = False
            prev_pane = ""
            stable_prompt = 0  # consecutive polls where a prompt is visible & stable
            for i in range(150):  # 300s total
                pane = capture_pane(session_name, lines=15)
                if is_idle_prompt(pane):
                    idle = True
                    break
                if has_prompt(pane) and pane == prev_pane:
                    stable_prompt += 1
                else:
                    stable_prompt = 0
                prev_pane = pane
                # 10 consecutive matching polls × 2s = 20s of prompt-visible
                # stability is a strong signal the pane is ready even if some
                # chrome glyph is still being interpreted as activity.
                if stable_prompt >= 10:
                    log.info(f"Cron '{job_id}': proceeding on stable-prompt fallback after {(i + 1) * 2}s")
                    idle = True
                    break
                await asyncio.sleep(2)
            if not idle:
                raise TimeoutError("Session busy for 300s, giving up")

            # Send the message
            message = job["message"]
            send_keys(session_name, message, literal=True)
            send_enter(session_name)

            # Poll for response with job-specific timeout
            timeout = job.get("timeout", 600)
            prev_content = ""
            prompt_count = 0
            elapsed = 0.0
            await asyncio.sleep(3)
            elapsed += 3

            while elapsed < timeout:
                pane_content = capture_pane(session_name, lines=15)
                if is_idle_prompt(pane_content):
                    prompt_count += 1
                else:
                    prompt_count = 0

                if elapsed >= 5 and prompt_count >= 5:
                    break

                await asyncio.sleep(POLL_INTERVAL)
                elapsed += POLL_INTERVAL
            else:
                log.warning(f"Cron '{job_id}' timed out after {timeout}s")

            # Extract response
            full_capture = capture_pane(session_name, lines=SCROLLBACK_LINES)
            response = self._extract_response(full_capture, message)

            if elapsed >= timeout:
                response += "\n\n⚠️ [TIMEOUT]"

            # Summarize if available
            if not self.raw_mode and self._api_available:
                try:
                    response = await asyncio.wait_for(
                        self._summarize(message, response),
                        timeout=60.0,
                    )
                except (asyncio.TimeoutError, Exception):
                    pass  # use raw response

            # Deliver to Telegram
            header = f"🕐 Cron: {job_id}\n\n"
            for chunk in split_message(header + response):
                try:
                    await bot.send_message(chat_id=CHAT_ID, text=chunk)
                except Exception:
                    async with httpx.AsyncClient(timeout=60) as client:
                        await client.post(
                            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                            json={"chat_id": CHAT_ID, "text": chunk},
                        )

            # Update job state
            job["last_run"] = now.isoformat()
            job["last_status"] = "ok" if elapsed < timeout else "timeout"
            self._save_cron_jobs()
            log.info(f"Cron '{job_id}' completed in {elapsed:.0f}s")

        except Exception as e:
            log.error(f"Cron '{job_id}' failed: {e}")
            job["last_run"] = datetime.now(ZoneInfo(job.get("tz", "Asia/Seoul"))).isoformat()
            job["last_status"] = f"error: {e}"
            self._save_cron_jobs()
            # Persist a forensic record for later review. Captures the tmux pane
            # so we can tell whether the failure was a trust prompt, auth loop,
            # infinite spinner, etc. without needing live reproduction.
            try:
                self._log_cron_error(job_id, job, e, session_name)
            except Exception as log_exc:
                log.warning(f"Could not persist cron error capture: {log_exc}")
            try:
                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=f"❌ Cron '{job_id}' failed: {e}\n(captured → ~/.liteclaw/cron-error-capture.md)",
                )
            except Exception:
                pass
        finally:
            self._cron_running.discard(job_id)

    def _log_cron_error(self, job_id: str, job: dict, exc: Exception, session_name: str):
        """Append a forensic markdown entry for a failed cron job."""
        path = LITECLAW_DIR / "cron-error-capture.md"
        try:
            LITECLAW_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        ts = datetime.now(ZoneInfo(job.get("tz", "Asia/Seoul"))).isoformat(timespec="seconds")
        # Pane snapshot (last 60 lines). Best-effort — the session may have
        # been killed already or never created.
        pane = "(no pane captured)"
        try:
            r = subprocess.run(
                ["tmux", "capture-pane", "-t", session_name, "-p", "-S", "-60"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                pane = r.stdout.rstrip() or "(pane empty)"
        except Exception:
            pass
        header_exists = path.exists() and path.stat().st_size > 0
        parts = []
        if not header_exists:
            parts.append("# LiteClaw cron error capture\n")
            parts.append("Each entry is a failed cron execution with the pane snapshot at the\n")
            parts.append("moment of failure. Append-only — newest at the bottom. Review later,\n")
            parts.append("fix the root cause per entry, and optionally delete the entry.\n\n")
        parts.append(f"## {ts} — `{job_id}`\n\n")
        parts.append(f"- **error**: `{exc}`\n")
        parts.append(f"- **cron**: `{job.get('cron_expr', '?')}` ({job.get('tz', '?')})\n")
        parts.append(f"- **project**: `{job.get('project', '?')}`\n")
        parts.append(f"- **timeout**: `{job.get('timeout', '?')}s`\n")
        parts.append(f"- **tmux session**: `{session_name}`\n\n")
        # Truncate pane to keep the file readable — 4 KB is plenty to see the
        # last few dozen lines of Claude Code output around the failure.
        if len(pane) > 4096:
            pane = "…(truncated)…\n" + pane[-4096:]
        parts.append("**pane snapshot**:\n\n")
        parts.append("```\n")
        parts.append(pane + ("\n" if not pane.endswith("\n") else ""))
        parts.append("```\n\n---\n\n")
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.writelines(parts)
            log.info(f"Cron error captured → {path}")
        except OSError as e:
            log.warning(f"Cron error capture write failed: {e}")

    def _agent_session_alive(self, session: str) -> bool:
        """Check if a tmux session is still running."""
        r = subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True,
        )
        return r.returncode == 0

    def _auth(self, update: Update) -> bool:
        """Check if message is from authorized chat."""
        return update.effective_chat.id == CHAT_ID

    # -- pipe-pane management --

    def _get_log_path(self) -> str:
        """Return the pipe-pane log file path for current target."""
        safe_name = self.target.replace(":", "_").replace(".", "_")
        return os.path.join(PIPE_LOG_DIR, f"liteclaw_{safe_name}.log")

    def _start_pipe(self):
        """Start tmux pipe-pane to capture output to a log file."""
        self._log_path = self._get_log_path()
        # Stop any existing pipe first
        subprocess.run(
            ["tmux", "pipe-pane", "-t", self.target],
            capture_output=True,
        )
        # Start new pipe
        subprocess.run(
            ["tmux", "pipe-pane", "-t", self.target, "-o",
             f"cat >> {self._log_path}"],
            check=True,
        )
        Path(self._log_path).touch()
        self._pipe_active = True
        log.info(f"pipe-pane started: {self._log_path}")

    def _stop_pipe(self):
        """Stop tmux pipe-pane."""
        subprocess.run(
            ["tmux", "pipe-pane", "-t", self.target],
            capture_output=True,
        )
        self._pipe_active = False

    def _mirror_diff(self, old: str, new: str) -> str:
        """Compute newly-appeared content in `new` versus `old` capture.

        If old is empty, return the last 15 non-empty lines of new.
        Otherwise, anchor on the last 3 non-empty lines of old found within
        new and return whatever follows that match.
        """
        new_nonempty = [l for l in new.split("\n") if l.strip()]
        if not new_nonempty:
            return ""
        if not old or not old.strip():
            return "\n".join(new_nonempty[-15:])

        old_nonempty = [l for l in old.split("\n") if l.strip()]
        if not old_nonempty:
            return "\n".join(new_nonempty[-15:])

        # Anchor on last 3 meaningful lines of old. Walk new from the end
        # looking for the same 3-line sequence.
        anchor = old_nonempty[-3:]
        anchor_len = len(anchor)
        for i in range(len(new_nonempty) - anchor_len, -1, -1):
            if new_nonempty[i:i + anchor_len] == anchor:
                tail = new_nonempty[i + anchor_len:]
                if tail:
                    return "\n".join(tail)
                return ""
        # No anchor found — likely scrolled out. Fall back to last 15 lines.
        return "\n".join(new_nonempty[-15:])

    async def _mirror_loop(self):
        """Periodically poll tmux pane and forward new CLI activity to Telegram.

        Respects self.busy (skip while normal request is in flight) and
        self._mirror_paused_until (skip right after we injected keys so our
        own echo is not mirrored back).
        """
        log.info("Mirror loop running")
        # Track last idle state — only mirror when Claude transitions to idle
        # and content has genuinely changed. Prevents spam during spinner updates.
        # Persist across restarts via self._last_mirror_hash (loaded from state).
        try:
            while self.mirror_on:
                await asyncio.sleep(MIRROR_POLL_INTERVAL)
                if not self.mirror_on:
                    break
                if self.busy:
                    continue
                if time.monotonic() < self._mirror_paused_until:
                    continue
                try:
                    capture = capture_pane(self.target, lines=40)
                except Exception as e:
                    log.debug(f"Mirror capture failed: {e}")
                    continue
                # Only mirror when Claude is idle (prompt visible, no spinner).
                if not is_idle_prompt(capture):
                    continue
                cleaned = clean_output(capture).strip()
                if not cleaned:
                    continue
                # Normalize content for STABLE hashing (strip timers/token counts/
                # cursor positions etc. that change even when "idle")
                recent_lines = [l for l in cleaned.split("\n") if l.strip()][-15:]
                recent = "\n".join(recent_lines)
                normalized = _normalize_for_mirror_hash(recent)
                idle_h = hashlib.md5(normalized.encode()).hexdigest()
                # Only fire on NEW idle state (content changed since last idle)
                if idle_h == self._last_mirror_hash:
                    continue
                # Require stability: confirm hash is same after short re-check
                # (prevents firing on transient idle->edit->idle oscillations)
                await asyncio.sleep(1.5)
                try:
                    recheck = capture_pane(self.target, lines=40)
                except Exception:
                    continue
                if not is_idle_prompt(recheck):
                    continue
                cleaned2 = clean_output(recheck).strip()
                recent_lines2 = [l for l in cleaned2.split("\n") if l.strip()][-15:]
                normalized2 = _normalize_for_mirror_hash("\n".join(recent_lines2))
                idle_h2 = hashlib.md5(normalized2.encode()).hexdigest()
                if idle_h2 != idle_h:
                    continue  # still changing, not truly stable
                new_content = self._mirror_diff(self._last_mirror_capture, cleaned)
                self._last_mirror_capture = cleaned
                self._last_mirror_hash = idle_h
                if not new_content or not new_content.strip():
                    continue
                if len(new_content) < 5:
                    continue  # too trivial
                if self._bot_ref is None:
                    continue
                # Summarize via 3-tier fallback (same as normal responses)
                # unless raw_mode is on, in which case forward as-is.
                summary = new_content
                if not self.raw_mode and len(new_content) > 100:
                    try:
                        summary = await asyncio.wait_for(
                            self._summarize("(CLI direct input — summarize what the user did and Claude's response)", new_content),
                            timeout=45,
                        )
                    except asyncio.TimeoutError:
                        log.warning("Mirror summarizer timeout, falling back to raw")
                        summary = new_content
                    except Exception as e:
                        log.warning(f"Mirror summarizer failed: {e}")
                        summary = new_content
                # Apply Telegram size limit (summary shouldn't be massive, but guard)
                if len(summary) > 3800:
                    summary = summary[:3800] + "\n\n…(truncated)"
                try:
                    await self._bot_ref.send_message(
                        chat_id=CHAT_ID,
                        text=f"🔁 CLI mirror\n\n{summary}",
                    )
                except Exception as e:
                    log.warning(f"Mirror send failed: {e}")
                await asyncio.sleep(MIRROR_DEBOUNCE)
        except asyncio.CancelledError:
            log.info("Mirror loop cancelled")
            raise
        except Exception:
            log.exception("Mirror loop crashed")

    def _record_offset(self):
        """Record current end of pipe log file."""
        if self._log_path and os.path.exists(self._log_path):
            self._log_offset = os.path.getsize(self._log_path)
        else:
            self._log_offset = 0

    def _load_current_session_id(self) -> str | None:
        """Read the active LiteClaw session UUID from ~/.liteclaw/sessions.json.

        Returns None if the file / key is missing. Safe to call at any time —
        used both at startup and as a lazy refresh when _log_conversation
        notices its cache is empty.
        """
        try:
            if LITECLAW_SESSIONS.exists():
                data = _json.loads(LITECLAW_SESSIONS.read_text(encoding="utf-8"))
                sid = data.get("liteclaw_session_id")
                if isinstance(sid, str) and sid:
                    return sid
        except Exception:
            pass
        return None

    def _current_session_alias(self) -> int | None:
        """Return the append-only integer index of the current session in
        sessions.json.history[]. Small enough to stamp on every transcript
        entry — a full UUID adds ~55 bytes per line; an int adds ~10.

        Returns None if no session id is allocated yet. history[] is
        append-only in _detect_resume_state, so indices are stable across
        restarts and resolve back to the UUID via sessions.json.
        """
        sid = self._current_session_id or self._load_current_session_id()
        if not sid:
            return None
        try:
            data = _json.loads(LITECLAW_SESSIONS.read_text(encoding="utf-8")) if LITECLAW_SESSIONS.exists() else {}
            history = data.get("history") or []
            for idx, h in enumerate(history):
                if h.get("id") == sid:
                    return idx
        except Exception:
            pass
        return None

    def _log_conversation(self, user_text: str, response: str, summarized: bool = False, meta: dict = None):
        """Append a conversation turn to JSONL history file.

        Writes both the legacy single-file ~/.liteclaw-history.jsonl (for
        back-compat with /recall) and the OpenClaw-style per-day transcript
        under ~/.liteclaw/transcripts/YYYY-MM-DD.jsonl. Each entry is tagged
        with the LiteClaw-owned session UUID so we can scope /recall session.
        """
        now = datetime.now()
        # Refresh cached session id once if it's still unknown — start.sh may
        # have allocated one after the daemon booted.
        if not self._current_session_id:
            self._current_session_id = self._load_current_session_id()
        # Compact alias: integer index into sessions.json.history[] (tiny vs
        # the ~55-byte UUID that would otherwise repeat on every turn).
        sid_alias = self._current_session_alias()
        entry = {
            "ts": now.isoformat(),
            "sid": sid_alias,
            "user": user_text[:500],  # cap to avoid bloat
            "response": response[:2000],  # keep summarized version (compact)
            "summarized": summarized,
        }
        if meta:
            entry["meta"] = meta
        line = _json.dumps(entry, ensure_ascii=False) + "\n"
        try:
            with open(HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            log.warning(f"Failed to write history: {e}")
        try:
            LITECLAW_TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
            daily = LITECLAW_TRANSCRIPTS / f"{now.strftime('%Y-%m-%d')}.jsonl"
            with open(daily, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            log.warning(f"Failed to write daily transcript: {e}")

    def _log_event(self, event_type: str, detail: str = ""):
        """Append behavioral event to ~/.liteclaw-events.jsonl"""
        try:
            entry = {"ts": datetime.now().isoformat(), "type": event_type, "detail": detail[:500]}
            with open(Path.home() / ".liteclaw-events.jsonl", "a") as f:
                f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _migrate_legacy_history(self) -> int:
        """One-shot: split legacy single-file history into per-day transcripts.

        Idempotent — if a daily file already exists with content, that day is
        skipped. Returns the number of entries migrated.
        """
        if not HISTORY_FILE.exists():
            return 0
        try:
            LITECLAW_TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning(f"Migration: cannot create {LITECLAW_TRANSCRIPTS}: {e}")
            return 0
        # Group entries by date and only append to days not yet populated.
        existing_days = {p.stem for p in LITECLAW_TRANSCRIPTS.glob("*.jsonl") if p.stat().st_size > 0}
        by_day: dict[str, list[str]] = {}
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    try:
                        entry = _json.loads(line)
                        ts = entry.get("ts", "")
                        day = ts[:10] if len(ts) >= 10 else "unknown"
                    except Exception:
                        day = "unknown"
                    if day in existing_days:
                        continue
                    by_day.setdefault(day, []).append(line + "\n")
        except OSError as e:
            log.warning(f"Migration: cannot read {HISTORY_FILE}: {e}")
            return 0
        moved = 0
        for day, lines in by_day.items():
            try:
                with open(LITECLAW_TRANSCRIPTS / f"{day}.jsonl", "a", encoding="utf-8") as f:
                    f.writelines(lines)
                moved += len(lines)
            except OSError as e:
                log.warning(f"Migration: cannot write {day}.jsonl: {e}")
        if moved:
            log.info(f"Migrated {moved} legacy history entries into {LITECLAW_TRANSCRIPTS}")
        return moved

    def _compact_day(self, day: str) -> dict:
        """Summarize ~/.liteclaw/transcripts/{day}.jsonl into memory/{day}.md.

        Synchronous, best-effort. Skips when:
          - the transcript doesn't exist or has <5 turns
          - the memory file already exists (idempotent)
          - the summarizer endpoint is unreachable
        Returns {day, status: 'created'|'skipped'|'failed', reason, bytes}.
        """
        result = {"day": day, "status": "skipped", "reason": "", "bytes": 0}
        src = LITECLAW_TRANSCRIPTS / f"{day}.jsonl"
        dst = LITECLAW_MEMORY / f"{day}.md"
        if not src.exists():
            result["reason"] = "no transcript"
            return result
        if dst.exists() and dst.stat().st_size > 0:
            result["reason"] = "already compacted"
            return result
        try:
            entries = []
            with open(src, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entries.append(_json.loads(line))
                    except Exception:
                        pass
        except OSError as e:
            result["reason"] = f"read failed: {e}"
            return result
        if len(entries) < 5:
            result["reason"] = f"only {len(entries)} turns"
            return result
        # Build a compact prompt — cap to keep token usage bounded.
        bullets = []
        for e in entries[-200:]:
            ts = (e.get("ts", "") or "")[:19]
            u = (e.get("user") or "").replace("\n", " ")[:300]
            a = (e.get("response") or "").replace("\n", " ")[:500]
            bullets.append(f"- [{ts}] U: {u}\n  A: {a}")
        body = "\n".join(bullets)
        sys_msg = (
            "You are a strategic memory compactor for an autonomous coding agent. "
            "Given a day of conversation turns (user msg + agent reply summaries), "
            "produce a Markdown digest with these sections:\n"
            "1) Goals & decisions made today\n"
            "2) Open threads / unfinished work\n"
            "3) Notable bugs, incidents, or surprises\n"
            "4) Useful facts the agent should remember tomorrow\n"
            "Be terse. Use bullets. Korean+English mix is fine. No fluff."
        )
        user_msg = f"Date: {day}\n\nTurns:\n{body}"
        try:
            with httpx.Client(timeout=45) as client:
                r = client.post(
                    f"{SUMMARIZER_URL}/chat/completions",
                    json={
                        "model": SUMMARIZER_MODEL,
                        "messages": [
                            {"role": "system", "content": sys_msg},
                            {"role": "user", "content": user_msg},
                        ],
                        "temperature": 0.3,
                    },
                )
            if r.status_code != 200:
                result["reason"] = f"http {r.status_code}"
                return result
            content = r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            result["reason"] = f"api: {e}"
            return result
        markdown = f"# {day} — daily memory\n_compacted: {datetime.now().isoformat(timespec='seconds')}_\n\n{content}\n"
        try:
            LITECLAW_MEMORY.mkdir(parents=True, exist_ok=True)
            dst.write_text(markdown, encoding="utf-8")
            result["status"] = "created"
            result["bytes"] = len(markdown.encode("utf-8"))
        except OSError as e:
            result["reason"] = f"write failed: {e}"
        return result

    def _build_primer(self) -> dict:
        """Build ~/.liteclaw/primer.md from strategic.md + recent N turns.

        Returns a dict {primer_path, size_bytes, recent_turns, has_strategic}
        usable by _send_boot_ready. Never raises — failures degrade silently.
        """
        info = {"path": str(LITECLAW_PRIMER), "size": 0, "recent": 0, "strategic": False}
        try:
            LITECLAW_DIR.mkdir(parents=True, exist_ok=True)
            LITECLAW_MEMORY.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning(f"Primer: cannot create dirs: {e}")
            return info
        # Recent: last N turns from the legacy history (cheapest source covering
        # all days). Once daily compaction is wired up, we can switch this to
        # tail of today's transcripts/<today>.jsonl.
        recent_lines: list[str] = []
        if HISTORY_FILE.exists():
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    tail = f.readlines()[-PRIMER_RECENT_TURNS:]
                for raw in tail:
                    try:
                        e = _json.loads(raw)
                    except Exception:
                        continue
                    ts = e.get("ts", "")[:19]
                    user = (e.get("user") or "").strip().replace("\n", " ")
                    resp = (e.get("response") or "").strip().replace("\n", " ")
                    recent_lines.append(f"- [{ts}] U: {user[:160]}\n  A: {resp[:240]}")
                info["recent"] = len(recent_lines)
            except OSError as e:
                log.warning(f"Primer: cannot read history: {e}")
        # Strategic: optional rolling summary, with fallback to 3 most recent
        # daily memory markdowns when no curated strategic.md exists yet.
        strategic_text = ""
        if LITECLAW_STRATEGIC.exists():
            try:
                strategic_text = LITECLAW_STRATEGIC.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        if not strategic_text and LITECLAW_MEMORY.is_dir():
            try:
                daily_mds = sorted(LITECLAW_MEMORY.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md"))
                chunks = []
                for md in daily_mds[-3:]:
                    try:
                        chunks.append(md.read_text(encoding="utf-8").strip())
                    except OSError:
                        continue
                strategic_text = "\n\n---\n\n".join(chunks)
            except OSError:
                pass
        info["strategic"] = bool(strategic_text)
        parts = [
            "# LiteClaw startup primer",
            f"_built: {datetime.now().isoformat(timespec='seconds')}_",
            "",
        ]
        if strategic_text:
            parts += ["## Strategic memory", strategic_text, ""]
        if recent_lines:
            parts += [f"## Recent {len(recent_lines)} turns", *recent_lines, ""]
        if not strategic_text and not recent_lines:
            parts.append("_(no prior context yet — clean start)_")
        body = "\n".join(parts) + "\n"
        try:
            LITECLAW_PRIMER.write_text(body, encoding="utf-8")
            info["size"] = len(body.encode("utf-8"))
        except OSError as e:
            log.warning(f"Primer: cannot write {LITECLAW_PRIMER}: {e}")
        return info

    # ---- Claude Code session JSONL reading --------------------------------
    # These helpers lift responses out of Claude's own session log at
    #   ~/.claude/projects/<encoded-cwd>/<liteclaw_session_id>.jsonl
    # instead of scraping the tmux TUI pane. The jsonl is append-only and
    # structured (role/content blocks), so we get clean text + a reliable
    # "this turn is done" signal via stop_reason.
    def _jsonl_path(self) -> Path | None:
        sid = self._current_session_id or self._load_current_session_id()
        if not sid:
            return None
        encoded = str(CLAUDE_CWD).replace("/", "-")
        p = Path.home() / ".claude" / "projects" / encoded / f"{sid}.jsonl"
        return p if p.exists() else None

    def _record_jsonl_offset(self) -> None:
        """Stamp the current jsonl size so we can tail from here after send."""
        try:
            p = self._jsonl_path()
            self._jsonl_offset = p.stat().st_size if p else 0
        except Exception:
            self._jsonl_offset = 0

    def _tail_jsonl_since_offset(self) -> tuple[str, bool, int]:
        """Read new jsonl content since `self._jsonl_offset`.

        Returns (concatenated_text, turn_complete, total_bytes_read_since_offset).
        text = concatenation of every assistant `text` block that appeared
               after the offset (across possibly many assistant messages in
               a single turn — e.g. tool_use → tool_result → more text).
        turn_complete = True iff the LAST assistant message observed has a
               stop_reason of end_turn / stop_sequence / max_tokens. A
               `tool_use` stop_reason means Claude wants a tool result next —
               still mid-turn, so we keep waiting.
        total_bytes_read_since_offset is used by the caller to detect file
        growth (Claude still writing) separately from text growth, so chains
        of tool_use-only messages don't trip the "stable" early-exit.
        """
        p = self._jsonl_path()
        if not p or not hasattr(self, "_jsonl_offset"):
            return ("", False, 0)
        try:
            size = p.stat().st_size
            if size <= self._jsonl_offset:
                return ("", False, 0)
            with open(p, "rb") as f:
                f.seek(self._jsonl_offset)
                chunk = f.read()
        except Exception as e:
            log.debug(f"jsonl tail read failed: {e}")
            return ("", False, 0)
        texts: list[str] = []
        last_stop_reason = None
        for raw in chunk.splitlines():
            if not raw.strip():
                continue
            try:
                obj = _json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                continue
            msg = obj.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text") or ""
                    if t:
                        texts.append(t)
            sr = msg.get("stop_reason")
            if sr:
                last_stop_reason = sr
        turn_complete = last_stop_reason in ("end_turn", "stop_sequence", "max_tokens")
        return ("\n".join(texts).strip(), turn_complete, len(chunk))

    async def _poll_response_via_jsonl(self, timeout: float = 600.0) -> str | None:
        """Wait up to `timeout` seconds for a complete assistant turn in jsonl.

        The completion signal is a stop_reason of end_turn / stop_sequence /
        max_tokens on the last assistant message in the new range. A chain of
        tool_use-only messages does NOT count as complete — we only return
        early on a true stop.

        Fallback "stable" safety net: if the jsonl file itself hasn't grown
        in >15 s AND we have some text, assume the writer has flushed and
        return what we have. File growth tracks BYTES (not just text blocks)
        so long tool_use chains with no intermediate text don't trip it.

        Side-effect: sets `self._jsonl_delivered_complete = True` when it
        returns on a genuine stop_reason, so handle_message can skip the
        follow-up monitor that would otherwise overwrite the delivered
        message with a stale status edit.
        """
        self._jsonl_delivered_complete = False
        if not USE_JSONL_RESPONSE:
            return None
        if not self._jsonl_path():
            return None
        deadline = time.monotonic() + timeout
        last_text = ""
        last_bytes = 0
        quiet_since: float | None = None
        while time.monotonic() < deadline:
            text, complete, bytes_read = self._tail_jsonl_since_offset()
            if complete and text:
                self._jsonl_delivered_complete = True
                return text
            if bytes_read > last_bytes:
                last_bytes = bytes_read
                quiet_since = None  # file still growing → still working
            elif text and quiet_since is None:
                quiet_since = time.monotonic()  # growth paused, start idle timer
            elif text and quiet_since is not None:
                # File quiet for ≥15 s AND we have some text → assume flushed.
                if time.monotonic() - quiet_since >= 15.0:
                    log.info(f"jsonl quiet-since safety net tripped after {time.monotonic() - quiet_since:.1f}s idle")
                    # Quiet-safety-net fired — text is likely final but we
                    # never observed a stop_reason. Treat as complete for
                    # follow-up suppression purposes (better than a stray
                    # status-edit obliterating the clean text).
                    self._jsonl_delivered_complete = True
                    return text
            if text and text != last_text:
                last_text = text
            await asyncio.sleep(1.0)
        return last_text or None

    def _detect_resume_state(self) -> dict:
        """Report on the LiteClaw-owned Claude Code session.

        start.sh allocates a stable UUID and stores it in
        ~/.liteclaw/sessions.json under `liteclaw_session_id`, then launches
        `claude --session-id <uuid>`. This pin makes resume deterministic even
        when the user runs other Claude Code windows in the same cwd.

        Also maintains `history[]` — appends a new entry whenever the
        current session UUID changes (e.g. after --fork-session), so strategic
        memory rollups can segment by session boundary instead of just by day.
        """
        info = {
            "resumable": False,
            "session_id": None,
            "session_path": None,
            "newest_age_min": None,
        }
        existing = {}
        if LITECLAW_SESSIONS.exists():
            try:
                existing = _json.loads(LITECLAW_SESSIONS.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        sess_id = existing.get("liteclaw_session_id")
        info["session_id"] = sess_id
        # Refresh the in-memory cache used by _log_conversation.
        if sess_id:
            self._current_session_id = sess_id
        # Maintain session lineage. Close the prior entry if the UUID changed,
        # and open a new one. History entries are append-only dicts.
        now_iso = datetime.now().isoformat(timespec="seconds")
        history = existing.get("history")
        if not isinstance(history, list):
            history = []
        last_entry = history[-1] if history else None
        last_id = last_entry.get("id") if last_entry else None
        if sess_id and sess_id != last_id:
            if last_entry and last_entry.get("ended_at") is None:
                last_entry["ended_at"] = now_iso
            history.append({
                "id": sess_id,
                "started_at": now_iso,
                "ended_at": None,
                "cwd": str(CLAUDE_CWD),
            })
            existing["history"] = history
        encoded = str(CLAUDE_CWD).replace("/", "-")
        proj_dir = Path.home() / ".claude" / "projects" / encoded
        if sess_id:
            sess_file = proj_dir / f"{sess_id}.jsonl"
            if sess_file.exists():
                age_s = time.time() - sess_file.stat().st_mtime
                info["resumable"] = True
                info["session_path"] = str(sess_file)
                info["newest_age_min"] = round(age_s / 60, 1)
        # Persist a refreshed snapshot, preserving start.sh's keys.
        try:
            LITECLAW_DIR.mkdir(parents=True, exist_ok=True)
            payload = dict(existing)
            payload.update({
                "cwd": str(CLAUDE_CWD),
                "encoded": encoded,
                "resumable": info["resumable"],
                "session_path": info["session_path"],
                "newest_age_min": info["newest_age_min"],
                "checked_at": datetime.now().isoformat(timespec="seconds"),
            })
            LITECLAW_SESSIONS.write_text(_json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass
        return info

    def _send_boot_ready(self, extras: dict = None):
        """Send a one-shot 'ready' Telegram message after bot init completes.

        Synchronous (uses httpx.Client) because this fires before app.run_polling
        takes over the event loop. Failures are logged but never block startup.
        Set BOOT_NOTIFY=0 in .env to disable.
        """
        if not BOOT_NOTIFY:
            log.info("Boot notify: disabled via BOOT_NOTIFY")
            # Still intentionally fall through below — we want the bookkeeping
            # (migrate, compact, primer build, resume state + history[]) to run
            # even when the ping is disabled. We'll bail at the actual send.
        # Compute rate-limit window (applied at send-time, not as an early
        # return — otherwise sessions.json/primer bookkeeping gets skipped).
        suppress_ping = not BOOT_NOTIFY
        try:
            last_boot_file = LITECLAW_DIR / ".last_boot_at"
            now_ts = time.time()
            if last_boot_file.exists():
                try:
                    prev = float(last_boot_file.read_text().strip())
                except Exception:
                    prev = 0.0
                if now_ts - prev < 300:  # 5 minutes
                    age = int(now_ts - prev)
                    log.info(f"Boot notify: suppressed (last ping {age}s ago)")
                    suppress_ping = True
            LITECLAW_DIR.mkdir(parents=True, exist_ok=True)
            last_boot_file.write_text(f"{now_ts}\n")
        except Exception as e:
            log.warning(f"Boot notify rate-limit check failed (continuing): {e}")
        try:
            import socket
            host = socket.gethostname()
        except Exception:
            host = "?"
        try:
            history_turns = sum(1 for _ in open(HISTORY_FILE, "r", encoding="utf-8")) if HISTORY_FILE.exists() else 0
        except OSError:
            history_turns = 0
        # Loop 4 enrichments: migrate legacy, compact yesterday, refresh primer,
        # probe resume state. All best-effort — failures must not block startup.
        try:
            self._migrate_legacy_history()
        except Exception as e:
            log.warning(f"Migration during boot notify failed: {e}")
        try:
            from datetime import timedelta
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            r = self._compact_day(yesterday)
            log.info(f"Compact {yesterday}: {r['status']} ({r.get('reason') or r.get('bytes')})")
        except Exception as e:
            log.warning(f"Compact day failed: {e}")
        primer = self._build_primer()
        resume = self._detect_resume_state()
        lines = [
            "🚀 LiteClaw ready",
            f"Host:    {host}",
            f"Target:  {self.target}",
            f"History: {history_turns} turns",
        ]
        sid = resume.get("session_id")
        sid_short = sid[:8] if sid else "?"
        if resume.get("resumable"):
            age = resume.get("newest_age_min")
            age_s = f", last touch {age}m ago" if age is not None else ""
            lines.append(f"Resume:  {sid_short} ({CLAUDE_CWD}{age_s})")
        elif sid:
            lines.append(f"Resume:  {sid_short} (new session — first run)")
        else:
            lines.append("Resume:  none (no session id allocated)")
        primer_kb = round(primer.get("size", 0) / 1024, 1)
        strat = "+strategic" if primer.get("strategic") else ""
        lines.append(f"Primer:  {primer.get('recent', 0)} recent turns{strat} ({primer_kb} KB)")
        if extras:
            for k, v in extras.items():
                lines.append(f"{k}: {v}")
        text = "\n".join(lines)
        if suppress_ping:
            return
        try:
            with httpx.Client(timeout=10) as client:
                r = client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
                )
                if r.status_code != 200:
                    log.warning(f"Boot notify failed: HTTP {r.status_code} {r.text[:200]}")
                else:
                    log.info(f"Boot notify sent ({len(text)} chars)")
        except Exception as e:
            log.warning(f"Boot notify exception: {e}")

    def _read_new_output(self) -> str:
        """Read new output from pipe log since last recorded offset."""
        if not self._log_path or not os.path.exists(self._log_path):
            return ""
        try:
            with open(self._log_path, "r", errors="replace") as f:
                f.seek(self._log_offset)
                return f.read()
        except OSError as e:
            log.warning(f"Failed to read pipe log: {e}")
            return ""

    # -- commands --

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return
        if self.raw_mode:
            mode = "raw"
        elif self._api_available:
            mode = f"API ({SUMMARIZER_MODEL})"
        else:
            mode = "agent (Tier 2)"
        await update.message.reply_text(
            "🔗 LiteClaw active\n"
            f"Target: `{self.target}` | Mode: {mode}\n\n"
            "Commands:\n"
            "/status — show last 30 lines\n"
            "/target SESSION:WIN.PANE — change target\n"
            "/cancel — send Ctrl+C\n"
            "/sessions — list tmux sessions\n"
            "/escape — send Escape key\n"
            "/raw — toggle raw/summarized output\n"
            "/model MODEL — change summarizer model\n"
            "/get FILEPATH — download a file\n"
            "/recall [N|keyword] — recall conversation history\n\n"
            "Multi-Agent:\n"
            "/agents — list all agents\n"
            "/agent new NAME PATH — create agent\n"
            "/agent status — detailed agent status\n"
            "/agent remove NAME — remove agent\n"
            "/assign NAME task — assign task to agent\n\n"
            "Cron:\n"
            "/cron list — show scheduled jobs\n"
            "/cron add — add a new job\n"
            "/cron run ID — manual trigger\n"
            "/cron enable|disable ID — toggle job",
            parse_mode="Markdown",
        )

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return
        try:
            content = capture_pane(self.target, lines=SCROLLBACK_LINES)
            lines = content.strip().split("\n")[-30:]
            text = clean_output("\n".join(lines))
            if not text.strip():
                text = "(pane is empty)"
            await update.message.reply_text(f"```\n{text}\n```", parse_mode="Markdown")
        except RuntimeError as e:
            await update.message.reply_text(f"Error: {e}")

    async def cmd_target(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return
        args = ctx.args
        if not args:
            await update.message.reply_text(
                f"Current target: `{self.target}`\nUsage: /target SESSION:WIN.PANE",
                parse_mode="Markdown",
            )
            return
        if self._pipe_active:
            self._stop_pipe()
        new_target = args[0]
        self.target = new_target
        self._start_pipe()
        self._log_event("target_switch", f"target={new_target}")
        await update.message.reply_text(
            f"Target changed to: `{self.target}`", parse_mode="Markdown",
        )

    async def cmd_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return
        try:
            send_keys(self.target, "C-c", literal=False)
            self.busy = False
            self._log_event("cancel")
            await update.message.reply_text("Sent Ctrl+C")
        except subprocess.CalledProcessError as e:
            await update.message.reply_text(f"Error: {e}")

    async def cmd_escape(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return
        try:
            send_keys(self.target, "Escape", literal=False)
            await update.message.reply_text("Sent Escape")
        except subprocess.CalledProcessError as e:
            await update.message.reply_text(f"Error: {e}")

    async def cmd_raw(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return
        self.raw_mode = not self.raw_mode
        self._log_event("mode_toggle", f"raw={'on' if self.raw_mode else 'off'}")
        mode = "ON (raw output)" if self.raw_mode else "OFF (summarized)"
        await update.message.reply_text(f"Raw mode: {mode}")

    async def cmd_mirror(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Toggle CLI mirror — forwards direct terminal activity to Telegram."""
        if not self._auth(update):
            return
        args = ctx.args or []
        if not args or args[0].lower() == "status":
            state = "ON" if self.mirror_on else "OFF"
            warn = "\n⚠️ Terminal output is being forwarded to Telegram" if self.mirror_on else ""
            await update.message.reply_text(
                f"🔁 CLI Mirror: *{state}*\n"
                f"Debounce: {MIRROR_DEBOUNCE}s | Poll: {MIRROR_POLL_INTERVAL}s"
                f"{warn}",
                parse_mode="Markdown",
            )
            return
        action = args[0].lower()
        if action == "on":
            self.mirror_on = True
            self._save_config()
            if self._bot_ref is None:
                self._bot_ref = ctx.bot
            if self._mirror_task is None or self._mirror_task.done():
                self._mirror_task = asyncio.create_task(self._mirror_loop())
            await update.message.reply_text(
                "🔁 CLI Mirror: *ON*\n⚠️ Terminal output will be forwarded to Telegram",
                parse_mode="Markdown",
            )
        elif action == "off":
            self.mirror_on = False
            self._save_config()
            if self._mirror_task and not self._mirror_task.done():
                self._mirror_task.cancel()
            await update.message.reply_text("🔁 CLI Mirror: *OFF*", parse_mode="Markdown")
        else:
            await update.message.reply_text("Usage: /mirror on|off|status")

    async def cmd_reasoning(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Toggle reasoning lane — split Thinking blocks into a separate message."""
        if not self._auth(update):
            return
        args = ctx.args or []
        action = args[0].lower() if args else "status"
        if action == "status":
            state = "ON" if self.reasoning_on else "OFF"
            detail = (
                "Thinking blocks will be separated"
                if self.reasoning_on
                else "Thinking blocks will appear inline"
            )
            await update.message.reply_text(
                f"{REASONING_PREFIX} Reasoning lane: *{state}*\n{detail}.",
                parse_mode="Markdown",
            )
        elif action == "on":
            self.reasoning_on = True
            self._save_config()
            await update.message.reply_text(
                f"{REASONING_PREFIX} Reasoning lane: *ON*", parse_mode="Markdown",
            )
        elif action == "off":
            self.reasoning_on = False
            self._save_config()
            await update.message.reply_text(
                f"{REASONING_PREFIX} Reasoning lane: *OFF*", parse_mode="Markdown",
            )
        else:
            await update.message.reply_text("Usage: /reasoning on|off|status")

    async def cmd_model(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return
        global SUMMARIZER_MODEL
        args = ctx.args
        if not args:
            await update.message.reply_text(
                f"Current: `{SUMMARIZER_MODEL}`\n"
                "Usage: /model claude-sonnet-4-6",
                parse_mode="Markdown",
            )
            return
        SUMMARIZER_MODEL = args[0]
        await update.message.reply_text(
            f"Summarizer model: `{SUMMARIZER_MODEL}`", parse_mode="Markdown",
        )

    async def cmd_sessions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return
        r = subprocess.run(["tmux", "list-sessions"], capture_output=True, text=True)
        text = r.stdout.strip() or "(no sessions)"
        await update.message.reply_text(f"```\n{text}\n```", parse_mode="Markdown")

    # -- multi-agent commands --

    async def cmd_agents(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """List all registered agents."""
        if not self._auth(update):
            return
        if not self._agents:
            await update.message.reply_text("No agents registered. Use /agent new <name> <path>")
            return

        lines = []
        for name, info in self._agents.items():
            alive = self._agent_session_alive(info["session"])
            if alive:
                # Check if agent is idle by looking for prompt
                try:
                    pane = capture_pane(info["session"], lines=15)
                    status = "idle" if has_prompt(pane) else "busy"
                except RuntimeError:
                    status = "error"
            else:
                status = "dead"
            info["status"] = status
            icon = {"idle": "🟢", "busy": "🟡", "dead": "🔴", "error": "🔴"}.get(status, "⚪")
            lines.append(f"{icon} {name} [{status}]\n   {info['project']}")

        self._save_agents()
        await update.message.reply_text("Agents:\n\n" + "\n\n".join(lines))

    async def cmd_agent(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle /agent subcommands: new, status, remove."""
        if not self._auth(update):
            return
        args = ctx.args
        if not args:
            await update.message.reply_text(
                "Usage:\n"
                "/agent new <name> <project_path>\n"
                "/agent status\n"
                "/agent remove <name>"
            )
            return

        subcmd = args[0].lower()

        if subcmd == "new":
            if len(args) < 3:
                await update.message.reply_text("Usage: /agent new <name> <project_path>")
                return
            name = args[1]
            project_path = " ".join(args[2:])  # allow spaces in path

            # Validate project path
            if not Path(project_path).is_dir():
                await update.message.reply_text(f"Error: directory not found: {project_path}")
                return

            if name in self._agents:
                existing = self._agents[name]
                if self._agent_session_alive(existing["session"]):
                    await update.message.reply_text(
                        f"Agent '{name}' already exists and is alive.\n"
                        "Use /agent remove <name> first."
                    )
                    return

            session_name = f"agent-{name}"

            # Create tmux session
            try:
                subprocess.run(
                    ["tmux", "new-session", "-d", "-s", session_name,
                     "-x", "200", "-y", "50"],
                    check=True, capture_output=True,
                )
                # cd to project path and start Claude Code
                subprocess.run(
                    ["tmux", "send-keys", "-t", session_name,
                     f"cd {project_path} && claude --dangerously-skip-permissions", "Enter"],
                    check=True,
                )
            except subprocess.CalledProcessError as e:
                await update.message.reply_text(f"Error creating tmux session: {e}")
                return

            self._agents[name] = {
                "session": session_name,
                "project": project_path,
                "status": "starting",
            }
            self._save_agents()
            self._log_event("agent_create", name)

            await update.message.reply_text(
                f"Agent '{name}' created.\n"
                f"Session: `{session_name}`\n"
                f"Project: `{project_path}`\n"
                "Waiting for Claude Code to start...",
                parse_mode="Markdown",
            )

            # Wait for Claude Code prompt
            for _ in range(20):  # max 20 seconds
                await asyncio.sleep(1)
                try:
                    content = capture_pane(session_name, lines=10)
                    if has_prompt(content):
                        self._agents[name]["status"] = "idle"
                        self._save_agents()
                        await update.message.reply_text(f"Agent '{name}' is ready.")
                        return
                except RuntimeError:
                    pass

            self._agents[name]["status"] = "unknown"
            self._save_agents()
            await update.message.reply_text(
                f"Agent '{name}' session created but Claude Code prompt not detected yet.\n"
                "It may still be starting. Try /agents to check."
            )

        elif subcmd == "status":
            if not self._agents:
                await update.message.reply_text("No agents registered.")
                return

            lines = []
            for name, info in self._agents.items():
                alive = self._agent_session_alive(info["session"])
                if alive:
                    try:
                        pane = capture_pane(info["session"], lines=15)
                        status = "idle" if has_prompt(pane) else "busy"
                        # Get last few lines as preview
                        preview_lines = clean_output(pane).strip().split("\n")[-3:]
                        preview = "\n".join(l for l in preview_lines if l.strip())
                    except RuntimeError:
                        status = "error"
                        preview = "(capture failed)"
                else:
                    status = "dead"
                    preview = "(session not found)"
                info["status"] = status
                icon = {"idle": "🟢", "busy": "🟡", "dead": "🔴", "error": "🔴"}.get(status, "⚪")
                lines.append(
                    f"{icon} {name} [{status}]\n"
                    f"   Session: {info['session']}\n"
                    f"   Project: {info['project']}\n"
                    f"   Preview: {preview[:200]}"
                )

            self._save_agents()
            await update.message.reply_text("Agent Status:\n\n" + "\n\n".join(lines))

        elif subcmd == "remove":
            if len(args) < 2:
                await update.message.reply_text("Usage: /agent remove <name>")
                return
            name = args[1]
            if name not in self._agents:
                await update.message.reply_text(f"Agent '{name}' not found.")
                return
            info = self._agents[name]
            # Kill tmux session if alive
            if self._agent_session_alive(info["session"]):
                subprocess.run(
                    ["tmux", "kill-session", "-t", info["session"]],
                    capture_output=True,
                )
            del self._agents[name]
            self._save_agents()
            self._log_event("agent_remove", name)
            await update.message.reply_text(f"Agent '{name}' removed.")

        else:
            await update.message.reply_text(
                f"Unknown subcommand: {subcmd}\n"
                "Usage: /agent new|status|remove"
            )

    async def cmd_cron(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle /cron subcommands: list, add, remove, enable, disable, run."""
        if not self._auth(update):
            return
        args = ctx.args
        if not args:
            await update.message.reply_text(
                "Usage:\n"
                "/cron list\n"
                "/cron add <id> <cron_expr(5)> <project> <message...>\n"
                "/cron remove <id>\n"
                "/cron enable <id>\n"
                "/cron disable <id>\n"
                "/cron run <id>\n"
                "/cron log <id>\n\n"
                "Example:\n"
                "/cron add daily-report 0 19 * * 1-5 ~/my-project Generate daily report"
            )
            return

        subcmd = args[0].lower()

        if subcmd == "list":
            if not self._cron_jobs:
                await update.message.reply_text("No cron jobs configured.")
                return
            lines = []
            for job in self._cron_jobs:
                icon = "✅" if job.get("enabled", True) else "⏸️"
                status = job.get("last_status", "never run")
                last = job.get("last_run", "-")
                if last and last != "-":
                    try:
                        last = last.split("T")[0] + " " + last.split("T")[1][:5]
                    except Exception:
                        pass
                running = " 🔄" if job["id"] in self._cron_running else ""
                lines.append(
                    f"{icon} {job['id']}{running}\n"
                    f"   Schedule: {job['cron_expr']} ({job.get('tz', 'Asia/Seoul')})\n"
                    f"   Project: {job.get('project', '~')}\n"
                    f"   Timeout: {job.get('timeout', 600)}s\n"
                    f"   Last: {last} [{status}]"
                )
            await update.message.reply_text("Cron Jobs:\n\n" + "\n\n".join(lines))

        elif subcmd == "add":
            # /cron add <id> <m> <h> <dom> <mon> <dow> <project> <message...>
            if len(args) < 9:
                await update.message.reply_text(
                    "Usage: /cron add <id> <min> <hour> <dom> <mon> <dow> <project> <message...>\n"
                    "Example: /cron add my-job 0 19 * * 1-5 ~/projects/foo Run the thing"
                )
                return
            job_id = args[1]
            cron_expr = " ".join(args[2:7])  # 5 cron fields
            project = args[7]
            message = " ".join(args[8:])

            if self._get_cron_job(job_id):
                await update.message.reply_text(f"Job '{job_id}' already exists. Remove it first.")
                return

            # Validate cron expression
            try:
                build_cron_trigger(cron_expr, "Asia/Seoul")
            except Exception as e:
                await update.message.reply_text(f"Invalid cron expression: {e}")
                return

            job = {
                "id": job_id,
                "enabled": True,
                "cron_expr": cron_expr,
                "tz": "Asia/Seoul",
                "message": message,
                "timeout": 600,
                "project": project,
                "last_run": None,
                "last_status": None,
            }
            self._cron_jobs.append(job)
            self._save_cron_jobs()

            # Register with scheduler if running
            if hasattr(ctx, "job_queue") and ctx.job_queue:
                try:
                    trigger = build_cron_trigger(cron_expr, "Asia/Seoul")
                    ctx.job_queue.run_custom(
                        callback=self._run_cron_job,
                        job_kwargs={"trigger": trigger, "id": f"cron-{job_id}", "replace_existing": True},
                        data=job,
                    )
                except Exception as e:
                    await update.message.reply_text(f"Saved but failed to schedule: {e}")
                    return

            self._log_event("cron_add", job_id)
            await update.message.reply_text(
                f"✅ Cron job '{job_id}' added.\n"
                f"Schedule: {cron_expr} (Asia/Seoul)\n"
                f"Project: {project}\n"
                f"Message: {message[:100]}{'...' if len(message) > 100 else ''}\n\n"
                "Restart LiteClaw to activate, or use /cron run to test."
            )

        elif subcmd == "remove":
            if len(args) < 2:
                await update.message.reply_text("Usage: /cron remove <id>")
                return
            job_id = args[1]
            job = self._get_cron_job(job_id)
            if not job:
                await update.message.reply_text(f"Job '{job_id}' not found.")
                return
            self._cron_jobs.remove(job)
            self._save_cron_jobs()
            # Kill tmux session if exists
            session_name = f"cron-{job_id}"
            if self._agent_session_alive(session_name):
                subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)
            # Remove from scheduler
            if ctx.job_queue:
                jobs = ctx.job_queue.get_jobs_by_name(f"cron-{job_id}")
                for j in jobs:
                    j.schedule_removal()
            self._log_event("cron_remove", job_id)
            await update.message.reply_text(f"✅ Cron job '{job_id}' removed.")

        elif subcmd in ("enable", "disable"):
            if len(args) < 2:
                await update.message.reply_text(f"Usage: /cron {subcmd} <id>")
                return
            job_id = args[1]
            job = self._get_cron_job(job_id)
            if not job:
                await update.message.reply_text(f"Job '{job_id}' not found.")
                return
            enabled = subcmd == "enable"
            job["enabled"] = enabled
            self._save_cron_jobs()

            if ctx.job_queue:
                if enabled:
                    try:
                        trigger = build_cron_trigger(job["cron_expr"], job.get("tz", "Asia/Seoul"))
                        ctx.job_queue.run_custom(
                            callback=self._run_cron_job,
                            job_kwargs={"trigger": trigger, "id": f"cron-{job_id}", "replace_existing": True},
                            data=job,
                        )
                    except Exception:
                        pass
                else:
                    jobs = ctx.job_queue.get_jobs_by_name(f"cron-{job_id}")
                    for j in jobs:
                        j.schedule_removal()

            icon = "✅" if enabled else "⏸️"
            await update.message.reply_text(f"{icon} Cron job '{job_id}' {'enabled' if enabled else 'disabled'}.")

        elif subcmd == "run":
            if len(args) < 2:
                await update.message.reply_text("Usage: /cron run <id>")
                return
            job_id = args[1]
            job = self._get_cron_job(job_id)
            if not job:
                await update.message.reply_text(f"Job '{job_id}' not found.")
                return
            if job_id in self._cron_running:
                await update.message.reply_text(f"Job '{job_id}' is already running.")
                return
            await update.message.reply_text(f"🚀 Running cron job '{job_id}'...")

            # Create a minimal context-like object for _run_cron_job
            class _FakeJobContext:
                def __init__(self, bot, data):
                    self.bot = bot
                    self.job = type("obj", (object,), {"data": data})()
            fake_ctx = _FakeJobContext(ctx.bot, job)
            asyncio.create_task(self._run_cron_job(fake_ctx))

        elif subcmd == "log":
            if len(args) < 2:
                await update.message.reply_text("Usage: /cron log <id>")
                return
            job_id = args[1]
            job = self._get_cron_job(job_id)
            if not job:
                await update.message.reply_text(f"Job '{job_id}' not found.")
                return
            last = job.get("last_run", "never")
            status = job.get("last_status", "never run")
            running = " (currently running)" if job_id in self._cron_running else ""
            await update.message.reply_text(
                f"Cron: {job_id}{running}\n"
                f"Last run: {last}\n"
                f"Status: {status}"
            )

        else:
            await update.message.reply_text(
                f"Unknown subcommand: {subcmd}\n"
                "Usage: /cron list|add|remove|enable|disable|run|log"
            )

    async def cmd_evolve(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle /evolve subcommands."""
        if not self._auth(update):
            return
        args = ctx.args or []
        subcmd = args[0].lower() if args else "status"

        if subcmd == "status":
            # Read evolve data files
            evolve_dir = Path.home() / "projects/liteclaw/.evolve/data"
            ideas_count = proposals_count = rules_count = 0
            pending_proposals = 0
            if evolve_dir.exists():
                ideas_file = evolve_dir / "ideas.jsonl"
                proposals_file = evolve_dir / "proposals.jsonl"
                rejections_file = evolve_dir / "rejections.jsonl"
                if ideas_file.exists():
                    ideas_count = sum(1 for _ in ideas_file.open())
                if proposals_file.exists():
                    for line in proposals_file.open():
                        proposals_count += 1
                        try:
                            p = _json.loads(line)
                            if p.get("decision") == "pending":
                                pending_proposals += 1
                        except Exception:
                            pass
                if rejections_file.exists():
                    rules_count = sum(1 for _ in rejections_file.open())

            skills_list = ", ".join(f"/{k}" for k in self._skills) or "(none)"
            text = (
                f"🧬 Evolution Status\n\n"
                f"Ideas: {ideas_count}\n"
                f"Proposals: {proposals_count} ({pending_proposals} pending)\n"
                f"Rejection rules: {rules_count}\n"
                f"Skills loaded: {skills_list}\n"
            )
            await update.message.reply_text(text)

        elif subcmd == "ideas":
            evolve_dir = Path.home() / "projects/liteclaw/.evolve/data"
            ideas_file = evolve_dir / "ideas.jsonl"
            if not ideas_file.exists():
                await update.message.reply_text("No ideas yet.")
                return
            ideas = []
            for line in ideas_file.open():
                try:
                    ideas.append(_json.loads(line))
                except Exception:
                    pass
            pending = [i for i in ideas if i.get("status") == "pending"][-5:]
            if not pending:
                await update.message.reply_text("No pending ideas.")
                return
            lines = []
            for i in pending:
                f = i.get("fitness", {})
                lines.append(f"• [{i['id'][:8]}] {i['title'][:60]} (F:{f.get('total', 0):.2f})")
            await update.message.reply_text("💡 Pending Ideas:\n\n" + "\n".join(lines))

        elif subcmd == "approve" and len(args) >= 2:
            proposal_id = args[1]
            await update.message.reply_text(f"⏳ Building proposal {proposal_id}...")
            self._log_event("evolve_approve", proposal_id)
            # Build runs in ephemeral session via cron system
            try:
                r = subprocess.run(
                    ["node", str(Path.home() / "projects/evolve-engine/dist/bin/evolve.js"), "build", proposal_id],
                    cwd=str(Path.home() / "projects/liteclaw"),
                    capture_output=True, text=True, timeout=300,
                )
                if r.returncode == 0:
                    await update.message.reply_text(f"✅ Build complete for {proposal_id}.\nCheck branch and merge manually to activate.")
                else:
                    await update.message.reply_text(f"❌ Build failed:\n{r.stderr[:500]}")
            except subprocess.TimeoutExpired:
                await update.message.reply_text("❌ Build timed out (300s)")
            except Exception as e:
                await update.message.reply_text(f"❌ Build error: {e}")

        elif subcmd == "reject" and len(args) >= 2:
            proposal_id = args[1]
            reason = " ".join(args[2:]) or "No reason given"
            self._log_event("evolve_reject", f"{proposal_id}: {reason}")
            try:
                r = subprocess.run(
                    ["node", str(Path.home() / "projects/evolve-engine/dist/bin/evolve.js"), "reject", proposal_id, reason],
                    cwd=str(Path.home() / "projects/liteclaw"),
                    capture_output=True, text=True, timeout=60,
                )
                if r.returncode == 0:
                    await update.message.reply_text(f"🚫 Rejected {proposal_id}. Rule learned.")
                else:
                    await update.message.reply_text(f"❌ Reject failed: {r.stderr[:300]}")
            except Exception as e:
                await update.message.reply_text(f"❌ Error: {e}")

        elif subcmd == "skill":
            skill_subcmd = args[1].lower() if len(args) > 1 else "list"
            if skill_subcmd == "list":
                if not self._skills:
                    await update.message.reply_text("No skills loaded.")
                    return
                lines = [f"• /{k} — {v['desc']} ({v['file']})" for k, v in self._skills.items()]
                await update.message.reply_text("🔧 Loaded Skills:\n\n" + "\n".join(lines))
            elif skill_subcmd == "reload":
                old_count = len(self._skills)
                self._load_skills()
                new_count = len(self._skills)
                await update.message.reply_text(f"♻️ Skills reloaded. {old_count} → {new_count}")
            else:
                await update.message.reply_text("Usage: /evolve skill list|reload")

        else:
            await update.message.reply_text(
                "Usage:\n"
                "/evolve status — Show evolution status\n"
                "/evolve ideas — Show pending ideas\n"
                "/evolve approve <id> — Approve proposal\n"
                "/evolve reject <id> [reason] — Reject proposal\n"
                "/evolve skill list|reload — Manage skills"
            )

    async def cmd_lcskill(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Manage LiteClaw skills: list | reload | new <name> | remove <name>."""
        if not self._auth(update):
            return
        args = ctx.args or []
        if not args:
            args = ["list"]
        sub = args[0].lower()

        if sub == "list":
            if not self._skills:
                await update.message.reply_text(
                    f"No skills loaded.\nAdd files to {SKILLS_PATH}"
                )
                return
            lines = [f"*LiteClaw Skills* ({len(self._skills)})"]
            for name, info in sorted(self._skills.items()):
                typ = info.get("type", "py")
                desc = info.get("desc", "")
                lines.append(f"/{name} _({typ})_ — {desc}")
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

        elif sub == "reload":
            self._load_skills()
            if self._bot_ref:
                await self._register_native_commands()
            await update.message.reply_text(f"✅ Reloaded {len(self._skills)} skills")

        elif sub == "new" and len(args) >= 2:
            name = args[1].lstrip("/")
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", name):
                await update.message.reply_text("Invalid name. Use [a-zA-Z][a-zA-Z0-9_]*")
                return
            path = SKILLS_PATH / f"{name}.md"
            if path.exists():
                await update.message.reply_text(f"Skill already exists: {path}")
                return
            template = (
                "---\n"
                f"command: {name}\n"
                "description: TODO describe this skill\n"
                "---\n"
                f"TODO: Write the prompt that Claude should execute when /{name} is invoked.\n"
                "Use {{args}} to reference arguments.\n"
            )
            try:
                path.write_text(template, encoding="utf-8")
            except Exception as e:
                await update.message.reply_text(f"❌ Failed to create skill: {e}")
                return
            await update.message.reply_text(
                f"✅ Created {path}\n\nEdit the file to add prompt, then run /lcskill reload",
                parse_mode="Markdown",
            )

        elif sub == "remove" and len(args) >= 2:
            name = args[1].lstrip("/")
            removed = False
            for ext in (".md", ".py"):
                p = SKILLS_PATH / f"{name}{ext}"
                if p.exists():
                    try:
                        p.unlink()
                        removed = True
                    except Exception as e:
                        await update.message.reply_text(f"❌ Failed to remove {p}: {e}")
                        return
            if removed:
                self._load_skills()
                if self._bot_ref:
                    await self._register_native_commands()
                await update.message.reply_text(f"🗑️ Removed skill: {name}")
            else:
                await update.message.reply_text(f"Not found: {name}")

        else:
            await update.message.reply_text(
                "Usage: /lcskill list | reload | new <name> | remove <name>"
            )

    async def _check_evolve_proposals(self, context):
        """Check for new pending proposals and notify via Telegram."""
        proposals_file = Path.home() / "projects/liteclaw/.evolve/data/proposals.jsonl"
        if not proposals_file.exists():
            return
        try:
            for line in proposals_file.open():
                try:
                    p = _json.loads(line)
                    if p.get("decision") == "pending" and p["id"] not in self._notified_proposals:
                        text = (
                            f"💡 New Evolution Proposal\n\n"
                            f"{p.get('summary', 'No summary')[:300]}\n\n"
                            f"ID: {p['id']}\n"
                            f"/evolve approve {p['id']}\n"
                            f"/evolve reject {p['id']} [reason]"
                        )
                        async with httpx.AsyncClient(timeout=10) as client:
                            await client.post(
                                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                                json={"chat_id": CHAT_ID, "text": text},
                            )
                        self._notified_proposals.add(p["id"])
                except Exception:
                    pass
        except Exception as e:
            log.debug(f"Proposal check error: {e}")

    async def cmd_assign(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Assign a task to an agent and poll for response."""
        if not self._auth(update):
            return
        args = ctx.args
        if not args or len(args) < 2:
            await update.message.reply_text("Usage: /assign <agent_name> <task text>")
            return

        agent_name = args[0]
        task_text = " ".join(args[1:])

        if agent_name not in self._agents:
            await update.message.reply_text(
                f"Agent '{agent_name}' not found.\n"
                f"Available: {', '.join(self._agents.keys()) or '(none)'}"
            )
            return

        info = self._agents[agent_name]
        session = info["session"]

        if not self._agent_session_alive(session):
            info["status"] = "dead"
            self._save_agents()
            await update.message.reply_text(
                f"Agent '{agent_name}' session is dead. Use /agent remove and recreate."
            )
            return

        # Check if agent is idle
        try:
            pane = capture_pane(session, lines=15)
            if not has_prompt(pane):
                preview = clean_output(pane).strip().split("\n")[-3:]
                await update.message.reply_text(
                    f"Agent '{agent_name}' is busy:\n```\n" +
                    "\n".join(preview) + "\n```\nWait for it to finish.",
                    parse_mode="Markdown",
                )
                return
        except RuntimeError as e:
            await update.message.reply_text(f"Error checking agent: {e}")
            return

        # Send task to agent
        info["status"] = "busy"
        self._save_agents()

        try:
            send_keys(session, task_text)
            send_enter(session)

            await update.message.reply_text(
                f"📤 Task sent to agent '{agent_name}'.\nWaiting for response..."
            )

            # Poll for response (reuse same pattern as _poll_response but targeting agent session)
            response = await self._poll_agent_response(ctx.bot, session, task_text)

            if response.strip():
                if not self.raw_mode:
                    response = await self._summarize(task_text, response)
                for chunk in split_message(response):
                    await update.message.reply_text(chunk)
            else:
                await update.message.reply_text(f"Agent '{agent_name}': (empty response)")

        except Exception as e:
            log.exception(f"Error assigning task to agent '{agent_name}'")
            await update.message.reply_text(f"Error: {e}")
        finally:
            info["status"] = "idle"
            self._save_agents()

    async def _poll_agent_response(self, bot, session: str, user_text: str) -> str:
        """Poll an agent's tmux session until response stabilizes."""
        prev_content = ""
        stable_count = 0
        prompt_count = 0  # consecutive polls where prompt is visible
        elapsed = 0.0
        last_typing = 0.0
        last_status = 0.0
        status_interval = INTERMEDIATE_INTERVAL
        status_msg_id = None

        await asyncio.sleep(2)
        elapsed += 2

        while MAX_WAIT == 0 or elapsed < MAX_WAIT:
            # Typing indicator every 4s
            if elapsed - last_typing >= 4:
                try:
                    await bot.send_chat_action(chat_id=CHAT_ID, action=ChatAction.TYPING)
                except Exception:
                    pass
                last_typing = elapsed

            # Prompt detection via capture-pane
            pane_content = capture_pane(session, lines=15)
            cleaned = clean_output(pane_content).strip()

            if cleaned == prev_content:
                stable_count += 1
            else:
                stable_count = 0
            prev_content = cleaned

            # Track consecutive idle prompt detections
            if is_idle_prompt(pane_content):
                prompt_count += 1
            else:
                prompt_count = 0

            # Status update at adaptive interval
            if elapsed - last_status >= status_interval:
                status_capture = capture_pane(session, lines=10)
                preview_text = clean_output(status_capture).strip()
                if preview_text:
                    preview_lines = [l for l in preview_text.split("\n") if l.strip()][-5:]
                    preview = "\n".join(preview_lines)
                    status_text = f"⏳ Agent working... ({int(elapsed)}s)\n\n{preview[:1500]}"
                    try:
                        if status_msg_id:
                            await bot.edit_message_text(
                                chat_id=CHAT_ID,
                                message_id=status_msg_id,
                                text=status_text,
                            )
                        else:
                            msg = await bot.send_message(
                                chat_id=CHAT_ID, text=status_text,
                            )
                            status_msg_id = msg.message_id
                    except Exception:
                        pass
                last_status = elapsed
                status_interval = min(status_interval + 5, 60)

            # Done: idle prompt detected for 5+ consecutive polls
            if elapsed >= 5 and prompt_count >= 5:
                log.info(f"Agent poll complete after {elapsed:.1f}s (stable_count={stable_count}, prompt_count={prompt_count})")
                break

            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
        else:
            log.warning(f"Agent poll timeout after {MAX_WAIT}s")

        # Clean up status message
        if status_msg_id:
            try:
                await bot.delete_message(chat_id=CHAT_ID, message_id=status_msg_id)
            except Exception:
                pass

        # Extract response
        full_capture = capture_pane(session, lines=SCROLLBACK_LINES)
        text = self._extract_response(full_capture, user_text)

        if MAX_WAIT > 0 and elapsed >= MAX_WAIT:
            text += "\n\n⚠️ [TIMEOUT — agent may still be working]"

        log.info(f"Agent response extracted: {len(text)} chars")
        return text

    async def cmd_get(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Send a file from the server to Telegram."""
        if not self._auth(update):
            return
        args = ctx.args
        if not args:
            await update.message.reply_text(
                "Usage: /get <filepath>\nRelative paths resolved from pane cwd.",
            )
            return

        filepath = args[0]
        if not os.path.isabs(filepath):
            cwd = get_pane_cwd(self.target)
            filepath = os.path.join(cwd, filepath)
        filepath = os.path.realpath(filepath)

        if not os.path.isfile(filepath):
            await update.message.reply_text(f"File not found: {filepath}")
            return

        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        if size_mb > 50:
            await update.message.reply_text(
                f"File too large ({size_mb:.1f}MB). Telegram limit is 50MB.",
            )
            return

        try:
            with open(filepath, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename=os.path.basename(filepath),
                )
        except Exception as e:
            await update.message.reply_text(f"Error sending file: {e}")

    async def cmd_recall(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Recall recent conversation history, optionally filtered by keyword.

        Usage:
            /recall                    — summarize last 20 conversations
            /recall 50                 — summarize last 50 conversations
            /recall keyword            — search + summarize matching conversations
            /recall session            — restrict to the current LiteClaw session
            /recall session <uuid>     — restrict to a specific past session UUID
            /recall session keyword    — session-scoped keyword search
        """
        if not self._auth(update):
            return

        if not HISTORY_FILE.exists():
            await update.message.reply_text("No conversation history yet.")
            return

        # Parse args: number, keyword, or session-scope.
        args = list(ctx.args) if ctx.args else []
        limit = HISTORY_RECALL_LIMIT
        keyword = None
        session_filter: str | None = None  # UUID or sentinel "__current__"
        if args and args[0].lower() == "session":
            args = args[1:]
            session_filter = "__current__"
            # Optional UUID (8+ hex chars with dashes) as 2nd token
            if args and re.fullmatch(r"[0-9a-fA-F-]{8,}", args[0]):
                session_filter = args[0]
                args = args[1:]
        if args:
            if args[0].isdigit():
                limit = min(int(args[0]), 200)
            else:
                keyword = " ".join(args).lower()

        # Resolve __current__ to the active UUID.
        if session_filter == "__current__":
            session_filter = self._current_session_id or self._load_current_session_id()
            if not session_filter:
                await update.message.reply_text(
                    "현재 세션 ID가 아직 없습니다 (start.sh 재실행 필요 또는 session_id 미기록 상태)."
                )
                return

        # Read history (tail for efficiency)
        try:
            lines = HISTORY_FILE.read_text(encoding="utf-8").strip().split("\n")
        except OSError as e:
            await update.message.reply_text(f"Error reading history: {e}")
            return

        entries = []
        for line in lines:
            if not line.strip():
                continue
            try:
                entries.append(_json.loads(line))
            except _json.JSONDecodeError:
                continue

        if session_filter:
            # Accept both legacy full-UUID entries and compact alias entries.
            # Resolve the filter to a candidate alias int via sessions.json.
            sid_alias_target: int | None = None
            try:
                data = _json.loads(LITECLAW_SESSIONS.read_text(encoding="utf-8")) if LITECLAW_SESSIONS.exists() else {}
                for idx, h in enumerate(data.get("history") or []):
                    if h.get("id") == session_filter:
                        sid_alias_target = idx
                        break
            except Exception:
                pass
            def _match(e):
                # New schema: "sid" is int
                if "sid" in e and sid_alias_target is not None and e["sid"] == sid_alias_target:
                    return True
                # Legacy schema: "session_id" was full UUID
                if e.get("session_id") == session_filter:
                    return True
                return False
            entries = [e for e in entries if _match(e)]

        if keyword:
            entries = [
                e for e in entries
                if keyword in e.get("user", "").lower()
                or keyword in e.get("response", "").lower()
            ]

        entries = entries[-limit:]

        if not entries:
            await update.message.reply_text("No matching conversations found.")
            return

        # Build context for summarizer
        conv_text = []
        for e in entries:
            ts = e.get("ts", "?")[:16]  # YYYY-MM-DDTHH:MM
            user = e.get("user", "")[:200]
            resp = e.get("response", "")[:300]
            conv_text.append(f"[{ts}] User: {user}\nClaude: {resp}")

        context_block = "\n---\n".join(conv_text)

        # Summarize with Haiku
        await update.message.reply_text(f"Recalling {len(entries)} conversations...")
        try:
            await ctx.bot.send_chat_action(chat_id=CHAT_ID, action=ChatAction.TYPING)
        except Exception:
            pass

        recall_prompt = (
            "You are reviewing a conversation history between a user and Claude Code (via Telegram bridge). "
            "Provide a concise summary of the key topics, decisions, and outcomes. "
            "Group by topic if possible. Use bullet points. Keep it under 2000 chars. "
            "If a keyword filter was used, focus on those conversations.\n\n"
            f"{'Keyword filter: ' + keyword + chr(10) if keyword else ''}"
            f"Conversations ({len(entries)} entries):\n{context_block[:8000]}"
        )

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{SUMMARIZER_URL}/chat/completions",
                    headers={"Authorization": "Bearer not-needed"},
                    json={
                        "model": SUMMARIZER_MODEL,
                        "messages": [{"role": "user", "content": recall_prompt}],
                        "max_tokens": 2000,
                    },
                )
                resp.raise_for_status()
                summary = resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            # Fallback: just show raw recent entries
            log.warning(f"Recall summarizer failed: {e}")
            summary = "Summarizer unavailable. Recent entries:\n\n"
            for e_item in entries[-10:]:
                summary += f"[{e_item.get('ts', '?')[:16]}] {e_item.get('user', '')[:100]}\n"

        for chunk in split_message(summary):
            await update.message.reply_text(chunk)

    # -- file receive handlers --

    async def handle_document(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle file uploads from Telegram — save and relay to Claude."""
        if not self._auth(update):
            return
        if self.busy:
            await update.message.reply_text("⏳ Still processing. Use /cancel to abort.")
            return

        doc = update.message.document
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        dest = STAGING_DIR / doc.file_name
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(str(dest))

        caption = update.message.caption or ""
        log.info(f"Received file: {doc.file_name} ({doc.file_size} bytes)")

        # For small text files, include content directly
        relay_msg = ""
        if doc.file_size < 50_000 and not _is_binary(dest):
            try:
                content = dest.read_text(errors="replace")
                relay_msg = (
                    f"The user shared a file '{doc.file_name}' with this content:\n"
                    f"```\n{content}\n```"
                )
            except Exception:
                pass

        if not relay_msg:
            relay_msg = f"The user shared a file: {doc.file_name} at {dest}"

        if caption:
            relay_msg += f"\n\nUser's message: {caption}"

        self.busy = True
        self._last_activity = datetime.now().isoformat()
        try:
            if not self._pipe_active:
                self._start_pipe()
            self._record_offset()

            send_keys(self.target, relay_msg)
            send_enter(self.target)

            await update.message.reply_text(
                f"📎 File saved: `{dest}`\n📤 Sent to Claude.", parse_mode="Markdown",
            )

            response = await self._poll_response(ctx.bot, relay_msg)

            if response.strip():
                if not self.raw_mode:
                    response = await self._summarize(caption or doc.file_name, response)
                for chunk in split_message(response):
                    await update.message.reply_text(chunk)
            else:
                await update.message.reply_text("(empty response)")
        except Exception as e:
            log.exception("Error handling document")
            await update.message.reply_text(f"Error: {e}")
        finally:
            self.busy = False

    async def handle_photo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle photo uploads from Telegram."""
        if not self._auth(update):
            return
        if self.busy:
            await update.message.reply_text("⏳ Still processing. Use /cancel to abort.")
            return

        photo = update.message.photo[-1]  # largest size
        STAGING_DIR.mkdir(parents=True, exist_ok=True)

        tg_file = await photo.get_file()
        ext = os.path.splitext(tg_file.file_path or "photo.jpg")[1] or ".jpg"
        filename = f"photo_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
        dest = STAGING_DIR / filename
        await tg_file.download_to_drive(str(dest))

        caption = update.message.caption or ""
        relay_msg = f"The user shared an image: {dest}"
        if caption:
            relay_msg += f"\nUser's message: {caption}"

        self.busy = True
        self._last_activity = datetime.now().isoformat()
        try:
            if not self._pipe_active:
                self._start_pipe()
            self._record_offset()

            send_keys(self.target, relay_msg)
            send_enter(self.target)

            await update.message.reply_text(
                f"📷 Photo saved: `{dest}`\n📤 Sent to Claude.", parse_mode="Markdown",
            )

            response = await self._poll_response(ctx.bot, relay_msg)

            if response.strip():
                if not self.raw_mode:
                    response = await self._summarize(caption or "photo", response)
                for chunk in split_message(response):
                    await update.message.reply_text(chunk)
            else:
                await update.message.reply_text("(empty response)")
        except Exception as e:
            log.exception("Error handling photo")
            await update.message.reply_text(f"Error: {e}")
        finally:
            self.busy = False

    # -- main message handler --

    async def handle_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Main handler: relay text to Claude Code and capture response."""
        if not self._auth(update):
            return

        user_text = update.message.text
        if not user_text:
            return

        # Free-form answer to a pending interactive prompt?
        # Must run BEFORE the busy check so the user can answer while bot is still polling.
        if getattr(self, '_interactive_sent', False) and self._last_interactive_options:
            idx = await self._interpret_user_answer(user_text)
            if idx is not None:
                try:
                    await update.message.reply_text(f"🎯 Interpreted as option {idx+1}")
                except Exception:
                    pass
                success = await self._click_option(idx)
                if success:
                    self._interactive_sent = False
                    self._last_interactive_options = []
                else:
                    try:
                        await update.message.reply_text(
                            "⚠️ Failed to dispatch selection. Please try again."
                        )
                    except Exception:
                        pass
                return
            else:
                try:
                    await update.message.reply_text(
                        "⚠️ Couldn't determine which option you meant. Please be more "
                        "specific (e.g. \"1번\", \"the second one\", or the option text)."
                    )
                except Exception:
                    pass
                return

        if self.busy:
            await update.message.reply_text("⏳ Still processing. Use /cancel to abort.")
            return

        t0 = time.monotonic()
        self.busy = True
        self._interactive_sent = False  # reset for new message
        self._last_interactive_options = []  # clear stale options
        self._last_activity = datetime.now().isoformat()
        log.info(f"Received: {user_text[:80]}...")

        # Cancel any running follow-up from previous message
        if self._followup_task and not self._followup_task.done():
            self._followup_task.cancel()
            log.info("Cancelled previous follow-up task")

        try:
            # Ensure pipe-pane is active
            if not self._pipe_active:
                self._start_pipe()

            # Check if Claude is idle or busy BEFORE sending
            pane_snapshot = capture_pane(self.target, lines=15)
            claude_idle = has_prompt(pane_snapshot)

            busy_msg = None
            if not claude_idle:
                status_lines = clean_output(pane_snapshot).strip().split("\n")[-5:]
                status_preview = "\n".join(l for l in status_lines if l.strip())
                busy_msg = await update.message.reply_text(
                    f"⚠️ Claude is currently busy:\n```\n{status_preview}\n```\n\n"
                    "Message queued — will notify when done.",
                    parse_mode="Markdown",
                )

            # Record offset before sending (both pipe-pane log and session jsonl)
            self._record_offset()
            self._record_jsonl_offset()

            # Snapshot pane BEFORE sending for reliable diff extraction
            pre_snapshot = capture_pane(self.target, lines=SCROLLBACK_LINES)

            # Send to tmux (Claude Code queues input even when busy)
            send_keys(self.target, user_text)
            send_enter(self.target)
            # Pause mirror briefly so the echoed injection is not forwarded back
            self._mirror_paused_until = time.monotonic() + 5.0

            sent_msg = None
            if claude_idle:
                sent_msg = await update.message.reply_text("⏳ 작업 중…")

            # Poll for response with streaming feedback
            response = await self._poll_response(ctx.bot, user_text, pre_snapshot=pre_snapshot)

            # Phase A: prefer the structured session jsonl — this bypasses
            # tmux pane scrollback truncation, ANSI/spinner chrome ("Thinking
            # (Crystalizing)…"), and the summarizer's over-compression. Falls
            # back silently to the pane-derived response if jsonl isn't
            # available (missing path, race, or unparseable).
            if USE_JSONL_RESPONSE:
                try:
                    # Generous timeout — tool-heavy turns can run 5+ minutes.
                    # The inner loop still exits the moment stop_reason is set.
                    jsonl_text = await self._poll_response_via_jsonl(timeout=600.0)
                except Exception as e:
                    log.warning(f"jsonl poll raised: {e}")
                    jsonl_text = None
                # Sanity guard: if jsonl somehow returned *much less* than the
                # pane-derived response, that usually means we returned the
                # preamble text only (tool_use chain still going). Prefer the
                # pane-derived version in that case so the user doesn't see a
                # truncated "이해했습니다…" stub.
                pane_len = len(response)
                jsonl_len = len(jsonl_text or "")
                if jsonl_text and (pane_len == 0 or jsonl_len >= pane_len * 0.5):
                    log.info(f"Response via jsonl: {jsonl_len} chars (replacing pane-derived {pane_len} chars)")
                    response = jsonl_text
                    # jsonl text is already clean — skip the summarizer pass.
                    self._skip_summarizer_once = True
                elif jsonl_text:
                    log.warning(
                        f"jsonl returned {jsonl_len} chars but pane has {pane_len} — keeping pane-derived"
                    )

            # Delete status messages before delivering final response
            for msg in (sent_msg, busy_msg):
                if msg:
                    try:
                        await ctx.bot.delete_message(chat_id=CHAT_ID, message_id=msg.message_id)
                    except Exception:
                        pass

            # Compute timing meta for conversation logging
            now = datetime.now()
            _conv_meta = {
                "response_ms": int((time.monotonic() - t0) * 1000),
                "raw_len": len(response),
                "target": self.target,
                "hour": now.hour,
                "weekday": now.weekday(),
            }

            # Deliver response (with persistent retry)
            delivered_msg_id = await self._deliver_response(response, user_text, update, ctx, meta=_conv_meta)

            # Schedule follow-up only when pane-scrape was the source. When the
            # jsonl path returned a complete (`stop_reason=end_turn`) turn,
            # there is literally no more assistant output coming — spinning up
            # the follow-up monitor just leads it to overwrite our clean final
            # message with a "⏳ Working... (Ns)" status edit (issue: user's
            # delivered message gets replaced by a short truncated one).
            skip_followup = bool(getattr(self, "_jsonl_delivered_complete", False))
            self._jsonl_delivered_complete = False  # one-shot reset
            if delivered_msg_id and not skip_followup:
                self._followup_task = asyncio.create_task(
                    self._followup_edit(user_text, pre_snapshot, delivered_msg_id, ctx.bot)
                )
            elif skip_followup:
                log.info("Skipping _followup_edit (jsonl turn already complete)")

        except RuntimeError as e:
            await update.message.reply_text(f"Error: {e}")
        except Exception as e:
            log.exception("Unexpected error in handle_message")
            # Last resort: try to extract and deliver whatever Claude has
            try:
                full_capture = capture_pane(self.target, lines=SCROLLBACK_LINES)
                if has_prompt(full_capture):
                    response = self._extract_response(full_capture, user_text)
                    if response.strip():
                        log.info("Recovering response after error...")
                        await self._deliver_response(response, user_text, update, ctx)
                    else:
                        await update.message.reply_text("(응답 추출 실패 — /status로 확인)")
                else:
                    await update.message.reply_text("⏳ Claude 아직 작업 중. 완료되면 알림 드립니다.")
                    # Keep polling in background until done
                    asyncio.create_task(self._background_deliver(user_text, ctx.bot))
            except Exception:
                pass
        finally:
            self.busy = False

    async def _deliver_response(self, response: str, user_text: str, update: Update, ctx: ContextTypes.DEFAULT_TYPE, meta: dict = None) -> int | None:
        """Deliver response to Telegram with retry. Summarizes unless raw mode. Returns last message_id.
        Phase C: If a draft message is active, edit it in-place with the final answer (single-chunk only).
        Phase D: If reasoning lane is enabled, extract thinking blocks and send them as a separate message
        BEFORE summarizing/delivering the answer.
        """
        if not response.strip():
            await update.message.reply_text("(empty response)")
            return None

        # Phase D: split reasoning from answer BEFORE summarization/chunking
        reasoning_text = ""
        if self.reasoning_on and REASONING_LANE_ENABLED:
            reasoning_text, response = _split_reasoning(response)

        # Phase D: send reasoning as its own message first, track id for follow-up
        if reasoning_text and self.reasoning_on and REASONING_LANE_ENABLED:
            try:
                reasoning_body = html.escape(reasoning_text[:3500])
                reasoning_msg = f"{REASONING_PREFIX} <i>Thinking</i>\n\n<pre>{reasoning_body}</pre>"
                msg = await ctx.bot.send_message(
                    chat_id=CHAT_ID, text=reasoning_msg, parse_mode="HTML",
                )
                self._reasoning_msg_id = msg.message_id
                log.info(f"Reasoning sent: {len(reasoning_text)} chars (msg_id={msg.message_id})")
            except Exception as e:
                log.warning(f"Reasoning send failed: {e}")

        # Guard: if the split left nothing to deliver, fall back to a minimal answer
        if not response.strip():
            response = "(reasoning only — no final answer extracted)"

        # Phase A: if jsonl gave us a clean response, skip the summarizer
        # entirely — no ANSI/spinner noise to strip, no content compression
        # to suffer. One-shot flag cleared immediately.
        skip_sum = getattr(self, "_skip_summarizer_once", False)
        self._skip_summarizer_once = False
        if skip_sum:
            log.info(f"Summarizer skipped (jsonl-sourced response, {len(response)} chars)")
        elif not self.raw_mode:
            try:
                await ctx.bot.send_chat_action(chat_id=CHAT_ID, action=ChatAction.TYPING)
            except Exception:
                pass
            log.info("Summarizer starting")
            try:
                response = await asyncio.wait_for(
                    self._summarize(user_text, response),
                    timeout=45.0,
                )
                log.info(f"Summarizer completed: {len(response)} chars")
            except asyncio.TimeoutError:
                log.warning("Summarizer timed out (45s), sending raw output")
                # response stays as raw — do not modify it

        chunks = split_message(response)
        log.info(f"Delivering response: {len(response)} chars in {len(chunks)} chunk(s)")

        # Phase C: try to edit the live draft message in place (single-chunk only)
        use_draft = bool(self.draft_on and DRAFT_STREAM_ENABLED and self._draft_msg_id)
        if use_draft:
            if len(chunks) == 1:
                ok = await self._edit_with_retry(
                    ctx.bot, CHAT_ID, self._draft_msg_id, chunks[0],
                )
                if ok:
                    last_msg_id = self._draft_msg_id
                    self._draft_msg_id = None
                    # Log + state-save (mirror legacy path tail)
                    if meta is not None:
                        meta["sum_len"] = len(response)
                    self._log_conversation(user_text, response, summarized=not self.raw_mode, meta=meta)
                    self._save_state()
                    log.info(f"Draft edited in-place: msg_id={last_msg_id} ({len(chunks[0])} chars)")
                    return last_msg_id
                # Edit failed — fall through to send-new path below
                log.warning("Draft edit-in-place failed, falling back to send-new")
            else:
                # Multi-chunk: delete draft, use legacy send path
                try:
                    await ctx.bot.delete_message(chat_id=CHAT_ID, message_id=self._draft_msg_id)
                except Exception:
                    pass
                self._draft_msg_id = None

        last_msg_id = None
        for i, chunk in enumerate(chunks):
            header = f"[{i+1}/{len(chunks)}]\n" if len(chunks) > 1 else ""
            for attempt in range(3):
                try:
                    sent = await update.message.reply_text(f"{header}{chunk}")
                    last_msg_id = sent.message_id
                    log.info(f"Delivery success: chunk {i+1}/{len(chunks)} ({len(chunk)} chars)")
                    break
                except Exception as e:
                    if attempt < 2:
                        log.warning(f"Telegram send retry {attempt+1}: {e}")
                        await asyncio.sleep(2 ** attempt)
                    else:
                        # Final fallback: direct API send (bypasses bot timeout settings)
                        log.warning(f"Telegram bot send failed 3x, using direct API fallback")
                        try:
                            async with httpx.AsyncClient(timeout=60) as client:
                                await client.post(
                                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                                    json={"chat_id": CHAT_ID, "text": f"{header}{chunk}"},
                                )
                            log.info(f"Direct API fallback success: chunk {i+1}/{len(chunks)}")
                        except Exception as e2:
                            log.error(f"Direct API send also failed: {e2}")
            if i < len(chunks) - 1:
                await asyncio.sleep(0.5)

        # Log conversation to history (add sum_len to meta if available)
        if meta is not None:
            meta["sum_len"] = len(response)
        self._log_conversation(user_text, response, summarized=not self.raw_mode, meta=meta)
        self._save_state()
        return last_msg_id

    async def _followup_edit(self, user_text: str, pre_snapshot: str, msg_id: int, bot):
        """Continuously monitor pane and edit delivered message until Claude truly finishes.
        Keeps updating even after apparent completion — re-checks to catch resumed work."""
        await asyncio.sleep(15)
        last_delivered_raw = ""  # raw text for comparison (pre-summarization)
        rounds_unchanged = 0  # consecutive rounds where RAW content didn't change
        elapsed = 0
        notified_late_edit = False  # only notify once about late updates

        while rounds_unchanged < 3:  # 3 consecutive unchanged = truly done (no time limit)
            # Wait for idle
            prompt_count = 0
            while True:
                try:
                    pane = capture_pane(self.target, lines=15)
                    if is_idle_prompt(pane):
                        prompt_count += 1
                    else:
                        prompt_count = 0
                        rounds_unchanged = 0  # Claude resumed — reset

                    if prompt_count >= 5:
                        break

                    elapsed += 5

                    # Status edit every 30s while waiting — only if no real content delivered yet
                    if elapsed % 30 == 0 and not last_delivered_raw:
                        status_text = clean_output(pane).strip()
                        preview = "\n".join(
                            [l for l in status_text.split("\n") if l.strip()][-3:]
                        )
                        if preview:
                            await self._edit_with_retry(
                                bot, CHAT_ID, msg_id,
                                f"⏳ Working... ({elapsed + 15}s)\n\n{preview[:3500]}",
                            )

                    await asyncio.sleep(5)
                except Exception as e:
                    log.warning(f"Follow-up wait error: {e}")
                    await asyncio.sleep(5)
                    elapsed += 5

            # Idle detected — extract and update
            try:
                full = capture_pane(self.target, lines=SCROLLBACK_LINES)
                new_raw = self._extract_response(full, user_text, pre_snapshot)
                if not new_raw.strip():
                    rounds_unchanged += 1
                    await asyncio.sleep(30)
                    elapsed += 30
                    continue

                # Compare RAW text (before summarization) to detect real changes
                if new_raw.strip() == last_delivered_raw.strip():
                    rounds_unchanged += 1
                    log.info(f"Follow-up: content unchanged (round {rounds_unchanged}/3)")
                    await asyncio.sleep(30)
                    elapsed += 30
                    continue

                # Content changed — summarize and update
                rounds_unchanged = 0
                last_delivered_raw = new_raw
                new_response = new_raw
                if not self.raw_mode:
                    try:
                        new_response = await asyncio.wait_for(
                            self._summarize(user_text, new_raw),
                            timeout=30.0,
                        )
                    except asyncio.TimeoutError:
                        pass
                chunks = split_message(new_response)
                if len(chunks) == 1:
                    ok = await self._edit_with_retry(
                        bot, CHAT_ID, msg_id, chunks[0],
                    )
                    if ok:
                        log.info(f"Follow-up edit: updated msg {msg_id} ({len(chunks[0])} chars)")
                else:
                    try:
                        await bot.delete_message(chat_id=CHAT_ID, message_id=msg_id)
                    except Exception:
                        pass
                    for chunk in chunks:
                        try:
                            sent = await bot.send_message(chat_id=CHAT_ID, text=chunk)
                            msg_id = sent.message_id
                        except Exception:
                            pass
                    log.info(f"Follow-up: replaced with {len(chunks)} new message(s)")

                # Notify user once if edit happened after 60s (they may have scrolled away)
                if elapsed >= 60 and not notified_late_edit:
                    notified_late_edit = True
                    try:
                        await bot.send_message(
                            chat_id=CHAT_ID,
                            text="📝 이전 응답이 업데이트되었습니다.",
                        )
                    except Exception:
                        pass

                self._log_conversation(user_text, new_response, summarized=not self.raw_mode)

            except Exception as e:
                log.warning(f"Follow-up edit error: {e}")

            await asyncio.sleep(30)
            elapsed += 30

        # Final update: capture one last time and deliver definitive response
        try:
            full = capture_pane(self.target, lines=SCROLLBACK_LINES)
            final_response = self._extract_response(full, user_text, pre_snapshot)
            if final_response.strip() and final_response.strip() != last_delivered_raw.strip():
                if not self.raw_mode:
                    try:
                        final_response = await asyncio.wait_for(
                            self._summarize(user_text, final_response),
                            timeout=30.0,
                        )
                    except asyncio.TimeoutError:
                        pass
                chunks = split_message(final_response)
                if len(chunks) == 1:
                    ok = await self._edit_with_retry(
                        bot, CHAT_ID, msg_id, chunks[0],
                    )
                    if ok:
                        log.info(f"Follow-up: final edit to msg {msg_id} ({len(chunks[0])} chars)")
                else:
                    try:
                        await bot.delete_message(chat_id=CHAT_ID, message_id=msg_id)
                    except Exception:
                        pass
                    for chunk in chunks:
                        try:
                            await bot.send_message(chat_id=CHAT_ID, text=chunk)
                        except Exception:
                            pass
                self._log_conversation(user_text, final_response, summarized=not self.raw_mode)
        except Exception as e:
            log.warning(f"Follow-up final update error: {e}")

        log.info(f"Follow-up: finalized after {elapsed + 15}s total")

    async def _background_deliver(self, user_text: str, bot):
        """Background task: wait for Claude to finish, then deliver response."""
        log.info("Background delivery started — waiting for Claude to finish")
        elapsed = 0.0
        prompt_count = 0  # consecutive polls where prompt is visible
        while elapsed < 1800:  # max 30 minutes
            await asyncio.sleep(5)
            elapsed += 5
            try:
                pane = capture_pane(self.target, lines=15)
                if is_idle_prompt(pane):
                    prompt_count += 1
                else:
                    prompt_count = 0
                    continue

                # Need 3+ consecutive prompt detections (15s) to confirm done
                if prompt_count < 3:
                    continue

                full = capture_pane(self.target, lines=SCROLLBACK_LINES)
                response = self._extract_response(full, user_text)
                if response.strip():
                    if not self.raw_mode:
                        log.info("Background delivery: summarizer starting")
                        try:
                            response = await asyncio.wait_for(
                                self._summarize(user_text, response),
                                timeout=45.0,
                            )
                            log.info(f"Background delivery: summarizer completed: {len(response)} chars")
                        except asyncio.TimeoutError:
                            log.warning("Background delivery: summarizer timed out, sending raw")
                    for chunk in split_message(response):
                        try:
                            await bot.send_message(chat_id=CHAT_ID, text=chunk)
                            log.info(f"Background delivery: sent chunk ({len(chunk)} chars)")
                        except Exception:
                            log.warning("Background delivery: bot send failed, using direct API")
                            async with httpx.AsyncClient(timeout=60) as client:
                                await client.post(
                                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                                    json={"chat_id": CHAT_ID, "text": chunk},
                                )
                    log.info(f"Background delivery complete: {len(response)} chars")
                    self._save_state()
                self.busy = False
                return
            except Exception as e:
                log.warning(f"Background delivery check failed: {e}")
        log.warning("Background delivery timeout (30min)")
        self.busy = False

    async def _send_interactive_prompt(self, bot, interactive: dict):
        """Send Claude's interactive prompt as Telegram inline keyboard."""
        question = interactive["question"]
        options = interactive["options"]

        # Build inline keyboard — one button per row
        keyboard = []
        for i, opt in enumerate(options):
            label = opt[:60]  # Telegram button limit
            keyboard.append([InlineKeyboardButton(label, callback_data=f"pick:{i}:{opt[:40]}")])

        markup = InlineKeyboardMarkup(keyboard)
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"❓ Claude is asking:\n\n**{question}**\n\nTap to select:",
                reply_markup=markup,
                parse_mode="Markdown",
            )
            log.info(f"Interactive prompt sent: {question} ({len(options)} options)")
        except Exception as e:
            log.warning(f"Failed to send interactive prompt: {e}")
            # Fallback: send as plain text with numbered options
            lines = [f"❓ Claude is asking:\n\n{question}\n"]
            for i, opt in enumerate(options, 1):
                lines.append(f"  {i}. {opt}")
            lines.append("\nReply with the number to select.")
            await bot.send_message(chat_id=CHAT_ID, text="\n".join(lines))

    async def _send_yn_prompt(self, bot, default: str, context_snippet: str):
        """Send a Y/N confirmation prompt as Telegram inline keyboard.

        default: "Y" (default yes), "N" (default no), or "?" (no default).
        context_snippet: recent pane lines to show what Claude is asking.
        """
        if default == "Y":
            buttons = [[
                InlineKeyboardButton("✅ Yes (default)", callback_data="yn:y"),
                InlineKeyboardButton("No", callback_data="yn:n"),
            ]]
            hint = "Press Yes (default) or No"
        elif default == "N":
            buttons = [[
                InlineKeyboardButton("Yes", callback_data="yn:y"),
                InlineKeyboardButton("❌ No (default)", callback_data="yn:n"),
            ]]
            hint = "Press No (default) or Yes"
        else:  # "?"
            buttons = [[
                InlineKeyboardButton("✅ Yes", callback_data="yn:y"),
                InlineKeyboardButton("❌ No", callback_data="yn:n"),
            ]]
            hint = "Please choose"
        markup = InlineKeyboardMarkup(buttons)
        preview = context_snippet[-500:] if context_snippet else ""
        text = f"❓ Claude is asking\n\n<pre>{html.escape(preview)}</pre>\n\n{hint}"
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=text,
                reply_markup=markup,
                parse_mode="HTML",
            )
            log.info(f"Y/N prompt sent (default={default})")
        except Exception as e:
            log.warning(f"Y/N prompt send failed: {e}")

    async def _click_option(self, idx: int) -> bool:
        """Navigate to option idx (0-based) and press Enter.
        idx==0 means first option (already pre-selected): press Enter only.
        Otherwise press Down idx times with verification, then Enter.
        Returns True on successful dispatch, False otherwise.
        """
        try:
            if idx < 0:
                return False
            # idx 0: first option is usually pre-selected — just Enter
            if idx == 0:
                send_keys(self.target, "Enter", literal=False)
                return True
            # Capture pane before navigation for verification
            pre = capture_pane(self.target, lines=20)
            for i in range(idx):
                send_keys(self.target, "Down", literal=False)
                await asyncio.sleep(DOWN_KEY_DELAY)
                post = capture_pane(self.target, lines=20)
                if post == pre:
                    # No visible change — may still be valid if render is lagged
                    log.warning(f"Down key {i+1}/{idx} showed no pane change")
                pre = post
            send_keys(self.target, "Enter", literal=False)
            return True
        except Exception as e:
            log.warning(f"_click_option failed: {e}")
            return False

    async def _handle_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard button presses for interactive prompts.

        Supports callback data prefixes:
          yn:y / yn:n         -> Y/N confirmation
          pick:{idx}:{label}  -> selection menu option
        """
        query = update.callback_query
        data = query.data or "" if query else ""
        if not query:
            return
        try:
            await query.answer()
        except Exception:
            pass

        try:
            # --- Y/N confirmation branch ---
            if data.startswith("yn:"):
                choice = data.split(":", 1)[1]  # "y" or "n"
                send_keys(self.target, choice, literal=True)
                await asyncio.sleep(0.1)
                send_keys(self.target, "Enter", literal=False)
                self._interactive_sent = False
                try:
                    await query.edit_message_text(f"✅ Sent: {choice.upper()}")
                except Exception:
                    pass
                log.info(f"Y/N callback handled: choice={choice}")
                return

            # --- Selection menu branch ---
            if data.startswith("pick:"):
                parts = data.split(":", 2)
                if len(parts) < 2:
                    try:
                        await query.edit_message_text("⚠️ Invalid callback data")
                    except Exception:
                        pass
                    return
                try:
                    idx = int(parts[1])
                except ValueError:
                    try:
                        await query.edit_message_text("⚠️ Invalid index")
                    except Exception:
                        pass
                    return
                label = parts[2] if len(parts) >= 3 else ""
                success = await self._click_option(idx)
                if success:
                    try:
                        if label:
                            await query.edit_message_text(f"✅ Selected option {idx+1}: {label}")
                        else:
                            await query.edit_message_text(f"✅ Selected option {idx+1}")
                    except Exception:
                        pass
                    log.info(f"Interactive selection: index={idx} label={label}")
                else:
                    # Callback failure — notify user and offer text-fallback
                    try:
                        await query.edit_message_text(
                            f"⚠️ Button failed — please reply with text\n"
                            f"(e.g. \"{idx+1}번\" or option text)"
                        )
                    except Exception:
                        pass
                    log.warning(f"Interactive selection dispatch failed: index={idx}")
                self._interactive_sent = False
                return

            # Unknown prefix
            try:
                await query.edit_message_text("⚠️ Unknown callback")
            except Exception:
                pass
        except Exception as e:
            log.warning(f"Callback handler error: {e}")

    async def _interpret_user_answer(self, answer: str) -> int | None:
        """Interpret a free-form text answer as an option index (0-based).
        Returns None if unclear or disabled. Uses cheap heuristics first,
        then falls back to the summarizer LLM for tricky cases."""
        if not INTERACTIVE_FREEFORM:
            return None
        opts = self._last_interactive_options
        if not opts:
            return None
        answer_lower = answer.strip().lower()
        if not answer_lower:
            return None

        # Quick numeric / ordinal heuristics (Korean + English)
        numeric_map = {
            "1": 0, "1번": 0, "첫": 0, "첫번째": 0, "첫번째꺼": 0, "first": 0,
            "2": 1, "2번": 1, "두번째": 1, "second": 1,
            "3": 2, "3번": 2, "세번째": 2, "third": 2,
            "4": 3, "4번": 3, "네번째": 3, "fourth": 3,
            "5": 4, "5번": 4, "다섯번째": 4, "fifth": 4,
        }
        for k, v in numeric_map.items():
            if (
                answer_lower == k
                or answer_lower.startswith(k + " ")
                or answer_lower.startswith(k + "번")
            ):
                if v < len(opts):
                    return v

        # Substring match: option text mentioned in answer (or vice versa)
        for i, opt in enumerate(opts):
            opt_lower = opt.lower().strip()
            if not opt_lower:
                continue
            if opt_lower in answer_lower or answer_lower in opt_lower:
                return i

        # LLM fallback via existing _summarize infrastructure
        try:
            options_text = "\n".join(f"{i+1}. {opt}" for i, opt in enumerate(opts))
            prompt = (
                f"User was shown this menu:\n{options_text}\n\n"
                f"User replied: \"{answer}\"\n\n"
                f"Which option number (1-{len(opts)}) does the user most likely want? "
                f"Respond with JUST the number, or 'unknown' if unclear."
            )
            response = await self._summarize(prompt, "")
            response = (response or "").strip()
            m = re.search(r"\b([1-9]\d?)\b", response)
            if m:
                num = int(m.group(1))
                if 1 <= num <= len(opts):
                    return num - 1
        except Exception as e:
            log.warning(f"LLM interpret failed: {e}")
        return None

    async def _poll_response(self, bot, user_text: str = "", pre_snapshot: str = "") -> str:
        """Poll until response stabilizes. Uses pipe-pane log for output, capture-pane for prompt detection.
        Phase C: When draft streaming is enabled, uses self._draft_msg_id to edit-in-place instead of
        sending a temporary status message that gets deleted.
        """
        prev_content = ""
        stable_count = 0
        prompt_count = 0  # consecutive polls where prompt is visible
        elapsed = 0.0
        last_typing = 0.0
        last_status = 0.0

        # Phase C: decide whether to use draft streaming (edit-in-place) or legacy (status msg + delete)
        use_draft = bool(self.draft_on and DRAFT_STREAM_ENABLED)
        if use_draft:
            status_interval = float(DRAFT_STREAM_INTERVAL)
            # Reset per-request draft state so we start fresh
            self._draft_last_hash = ""
            self._draft_last_edit_at = 0.0
            # self._draft_msg_id stays None until first preview send; _deliver_response consumes it
        else:
            status_interval = INTERMEDIATE_INTERVAL  # starts at 10s, grows to 60s
        status_msg_id = None  # legacy-only: temp status message id to delete at end
        preview_msg = ""       # last rendered preview text (used for TIMEOUT decoration)

        await asyncio.sleep(2)
        elapsed += 2

        while MAX_WAIT == 0 or elapsed < MAX_WAIT:
            # Typing indicator every 4s
            if elapsed - last_typing >= 4:
                try:
                    await bot.send_chat_action(chat_id=CHAT_ID, action=ChatAction.TYPING)
                except Exception:
                    pass
                last_typing = elapsed

            # Prompt detection via capture-pane (small window, fast)
            pane_content = capture_pane(self.target, lines=15)

            # Stability check: compare only last 3 lines (prompt area)
            # Full pane changes during typing but prompt area stabilizes when done
            cleaned_tail = "\n".join(clean_output(pane_content).strip().split("\n")[-3:])
            if cleaned_tail == prev_content:
                stable_count += 1
            else:
                stable_count = 0
            prev_content = cleaned_tail

            # Track consecutive idle prompt detections
            # is_idle_prompt checks both prompt presence AND absence of activity
            if is_idle_prompt(pane_content):
                prompt_count += 1
            else:
                prompt_count = 0

            # Interactive prompt detection: stable + no activity + not idle = waiting for input
            _recent = "\n".join(pane_content.strip().split("\n")[-10:])
            if stable_count >= 3 and not _ACTIVITY_PATTERNS.search(_recent) and not is_idle_prompt(pane_content):
                # Y/N confirmation detection runs first (cheaper, more specific)
                if INTERACTIVE_AUTO_YN and not getattr(self, '_interactive_sent', False):
                    yn = _detect_yn_prompt(pane_content)
                    if yn:
                        self._interactive_sent = True
                        snippet = "\n".join(
                            [l for l in pane_content.split("\n") if l.strip()][-8:]
                        )
                        await self._send_yn_prompt(bot, yn, snippet)

                interactive = detect_interactive_prompt(pane_content)
                if interactive and not getattr(self, '_interactive_sent', False):
                    self._interactive_sent = True
                    # Save options for free-form answer parser
                    self._last_interactive_options = list(interactive.get("options", []))
                    await self._send_interactive_prompt(bot, interactive)

            # Status update at adaptive interval — show last few meaningful lines.
            # Gated by SHOW_POLLING_STATUS: off by default because this path is
            # what surfaced "Thinking (Crystalizing)…" TUI chrome in Telegram.
            if SHOW_POLLING_STATUS and elapsed - last_status >= status_interval:
                last_status = elapsed
                status_capture = capture_pane(self.target, lines=15)
                preview_text = clean_output(status_capture).strip()
                if preview_text:
                    preview_lines = [l for l in preview_text.split("\n") if l.strip()][-5:]
                    preview = "\n".join(preview_lines)

                    if use_draft:
                        # Phase C: hash-gate + 1 edit/sec rate limit + edit-in-place
                        preview_msg = f"💭 Working...\n\n<pre>{html.escape(preview[-2000:])}</pre>"
                        h = hashlib.md5(preview.encode()).hexdigest()
                        skip = False
                        if h == self._draft_last_hash:
                            skip = True
                        now = time.monotonic()
                        if not skip and (now - self._draft_last_edit_at) < 1.0:
                            skip = True
                        if not skip:
                            self._draft_last_hash = h
                            self._draft_last_edit_at = now
                            if self._draft_msg_id is None:
                                try:
                                    msg = await bot.send_message(
                                        chat_id=CHAT_ID,
                                        text=preview_msg,
                                        parse_mode="HTML",
                                    )
                                    self._draft_msg_id = msg.message_id
                                except Exception as e:
                                    msg_l = str(e).lower()
                                    if "too many requests" in msg_l or "flood" in msg_l:
                                        # 429 backoff: bump interval to 12s
                                        status_interval = max(status_interval, 12.0)
                                    log.warning(f"Draft send failed: {e}")
                            else:
                                ok = await self._edit_with_retry(
                                    bot, CHAT_ID, self._draft_msg_id,
                                    preview_msg, parse_mode="HTML",
                                )
                                if not ok:
                                    # persistent edit failure — treat like 429 backoff
                                    status_interval = max(status_interval, 12.0)
                        # Adaptive interval grows but caps at 4x configured interval
                        status_interval = min(status_interval + 2, DRAFT_STREAM_INTERVAL * 4)
                    else:
                        # Legacy path: temp status message, delete at end
                        status_text = f"⏳ Working... ({int(elapsed)}s)\n\n{preview[:1500]}"
                        try:
                            if status_msg_id:
                                await bot.edit_message_text(
                                    chat_id=CHAT_ID,
                                    message_id=status_msg_id,
                                    text=status_text,
                                )
                            else:
                                msg = await bot.send_message(
                                    chat_id=CHAT_ID, text=status_text,
                                )
                                status_msg_id = msg.message_id
                        except Exception:
                            pass
                        status_interval = min(status_interval + 5, 60)

            # Done: idle prompt confirmed (~7.5s continuous idle)
            if elapsed >= 5 and prompt_count >= 5:
                log.info(f"Poll complete after {elapsed:.1f}s (stable_count={stable_count}, prompt_count={prompt_count})")
                break

            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
        else:
            log.warning(f"Timeout after {MAX_WAIT}s")

        # Clean up status message (legacy path only). In draft path we keep _draft_msg_id
        # so _deliver_response can edit it in place with the final answer.
        if status_msg_id:
            try:
                await bot.delete_message(chat_id=CHAT_ID, message_id=status_msg_id)
            except Exception:
                pass

        # On timeout under draft streaming, decorate the draft with a TIMEOUT prefix
        # so the user sees the last preview rather than a disappearing status.
        if use_draft and MAX_WAIT > 0 and elapsed >= MAX_WAIT and self._draft_msg_id:
            try:
                timeout_msg = (
                    f"⚠️ TIMEOUT after {MAX_WAIT}s\n\n{preview_msg}"
                    if preview_msg
                    else f"⚠️ TIMEOUT after {MAX_WAIT}s"
                )
                await self._edit_with_retry(
                    bot, CHAT_ID, self._draft_msg_id,
                    timeout_msg, parse_mode="HTML",
                )
            except Exception:
                pass

        # Extract response using capture-pane (rendered output, not raw pipe)
        full_capture = capture_pane(self.target, lines=SCROLLBACK_LINES)
        text = self._extract_response(full_capture, user_text, pre_snapshot)

        if MAX_WAIT > 0 and elapsed >= MAX_WAIT:
            text += "\n\n⚠️ [TIMEOUT — Claude may still be working]"

        log.info(f"Response extracted: {len(text)} chars")
        return text

    def _extract_response(self, capture: str, user_text: str, pre_snapshot: str = "") -> str:
        """Extract Claude's response from capture-pane.
        Primary: diff against pre_snapshot (reliable).
        Fallback: echo matching (legacy).

        Note: Phase D's `_split_reasoning` is a module-level helper defined after this class.
        """
        cleaned = clean_output(capture)
        lines = cleaned.split("\n")

        # === Strategy 0: Diff against pre-snapshot (most reliable) ===
        if pre_snapshot:
            pre_cleaned = clean_output(pre_snapshot).strip()
            pre_lines = pre_cleaned.split("\n")
            # Find where pre-snapshot ends in current capture
            # Match last 3 non-empty lines from pre-snapshot
            anchor_lines = [l for l in pre_lines if l.strip()][-3:]
            if anchor_lines:
                for i in range(len(lines) - len(anchor_lines), -1, -1):
                    if lines[i:i+len(anchor_lines)] == anchor_lines:
                        response_lines = lines[i+len(anchor_lines):]
                        # Skip the echoed user input (first non-empty line after anchor)
                        while response_lines and not response_lines[0].strip():
                            response_lines.pop(0)
                        if response_lines:
                            response_lines.pop(0)  # skip user echo line
                        # Clean trailing prompt
                        while response_lines and response_lines[-1].strip().strip("\xa0") in ("❯", ""):
                            response_lines.pop()
                        while response_lines and not response_lines[0].strip():
                            response_lines.pop(0)
                        result = "\n".join(response_lines).strip()
                        if result:
                            log.info(f"Response extracted via pre-snapshot diff: {len(result)} chars")
                            return result

        # === Strategy 1-3: Echo matching (fallback) ===
        search_text = user_text[:50].strip()
        user_echo_idx = -1

        if search_text:
            # Find line with ❯ prompt + user text
            for i in range(len(lines) - 1, -1, -1):
                if "❯" in lines[i] and search_text in lines[i]:
                    user_echo_idx = i
                    break
            # Find user text after a ❯ line
            if user_echo_idx < 0:
                for i in range(len(lines) - 1, 0, -1):
                    if search_text in lines[i] and "❯" in lines[i - 1]:
                        user_echo_idx = i
                        break
            # Find user text anywhere
            if user_echo_idx < 0:
                for i in range(len(lines) - 1, -1, -1):
                    if search_text in lines[i]:
                        user_echo_idx = i
                        break

        if user_echo_idx >= 0:
            response_lines = lines[user_echo_idx + 1:]
        else:
            log.warning("Could not find user echo in capture-pane, using prompt-pair fallback")
            prompt_indices = [i for i, l in enumerate(lines) if "❯" in l.strip()]
            if len(prompt_indices) >= 2:
                start = prompt_indices[-2] + 1
                end = prompt_indices[-1]
                response_lines = lines[start:end]
            elif prompt_indices:
                response_lines = lines[:prompt_indices[-1]]
            else:
                response_lines = lines

        # Clean trailing prompt and empty lines
        while response_lines and response_lines[-1].strip().strip("\xa0") in ("❯", ""):
            response_lines.pop()
        while response_lines and not response_lines[0].strip():
            response_lines.pop(0)

        return "\n".join(response_lines).strip()

    async def _probe_api(self) -> bool:
        """Check if the API summarizer (Tier 1) is reachable."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{SUMMARIZER_URL}/models")
                return resp.status_code == 200
        except Exception:
            return False

    async def _recover_proxy(self) -> bool:
        """Try to restart the API proxy. macOS uses LaunchAgent; Linux uses Docker compose."""
        log.warning("Attempting to restart max-api-proxy...")
        try:
            if sys.platform == "darwin":
                # Docker on macOS cannot access the keychain where Claude CLI stores OAuth
                # tokens, so the proxy runs as a LaunchAgent instead. See MAC-OPS.md.
                label = f"gui/{os.getuid()}/com.claude-max-api-proxy"
                r = subprocess.run(
                    ["launchctl", "kickstart", "-k", label],
                    capture_output=True, text=True, timeout=15,
                )
                settle_s = 5
            else:
                r = subprocess.run(
                    ["docker", "compose", "up", "-d"],
                    cwd=os.environ.get("PROXY_DIR", os.path.expanduser("~/max_api_proxy")),
                    capture_output=True, text=True, timeout=30,
                )
                settle_s = 3
            if r.returncode == 0:
                await asyncio.sleep(settle_s)
                if await self._probe_api():
                    log.info("max-api-proxy recovered successfully")
                    await self._notify_recovery("max-api-proxy restarted")
                    return True
            else:
                log.warning(f"Proxy recovery returncode={r.returncode} stderr={r.stderr[:200]}")
        except Exception as e:
            log.warning(f"Proxy recovery failed: {e}")
        return False

    def _check_session_401(self, session: str) -> bool:
        """Check if a tmux session shows 401 auth error."""
        try:
            content = capture_pane(session, lines=10)
            return "401" in content and ("authentication" in content.lower() or "Please run /login" in content)
        except Exception:
            return False

    async def _recover_session_auth(self, session: str) -> bool:
        """Send /login to a Claude Code session and forward OAuth URL to Telegram."""
        log.warning(f"Attempting to re-authenticate session: {session}")
        try:
            send_keys(session, "/login")
            send_enter(session)

            # Wait for login to produce OAuth URL
            await asyncio.sleep(5)

            # Extract OAuth URL from pane output
            oauth_url = None
            for _ in range(10):
                content = capture_pane(session, lines=30)
                # Look for OAuth/login URL patterns
                for line in content.split("\n"):
                    line = line.strip()
                    if re.search(r"https://[^\s]*(?:oauth|authorize|login|auth)[^\s]*", line):
                        match = re.search(r"(https://[^\s]+)", line)
                        if match:
                            oauth_url = match.group(1)
                            break
                if oauth_url:
                    break
                await asyncio.sleep(2)

            if oauth_url:
                log.info(f"OAuth URL found, forwarding to Telegram")
                await self._send_oauth_url(session, oauth_url)
            else:
                log.info("No OAuth URL detected, checking if auto-login succeeded...")

            # Wait for re-auth to complete (user clicks link on phone, or auto-login)
            for _ in range(60):  # 2 min window for user to click
                await asyncio.sleep(2)
                content = capture_pane(session, lines=10)
                if has_prompt(content) and "401" not in content:
                    log.info(f"Session {session} re-authenticated")
                    await self._notify_recovery(f"Session {session} re-authenticated")
                    return True
            log.warning(f"Re-authentication timed out for {session}")
        except Exception as e:
            log.warning(f"Re-auth failed for {session}: {e}")
        return False

    async def _send_oauth_url(self, session: str, url: str):
        """Send OAuth login URL to Telegram so user can approve on phone."""
        text = (
            f"🔐 Session '{session}' needs re-authentication.\n\n"
            f"Tap the link below to approve:\n{url}\n\n"
            f"Waiting up to 2 minutes for approval..."
        )
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": CHAT_ID, "text": text},
                )
        except Exception as e:
            log.warning(f"Failed to send OAuth URL to Telegram: {e}")

    async def _heartbeat_check(self, context):
        """Periodic auth health check. Detects expired sessions early."""
        try:
            r = subprocess.run(
                ["claude", "auth", "status"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0 or '"loggedIn": false' in r.stdout or "error" in r.stdout.lower():
                log.warning("Heartbeat: auth expired, attempting recovery")
                await self._notify_recovery("Auth expired — initiating re-login...")
                session = TMUX_TARGET
                await self._recover_session_auth(session)
            else:
                log.debug("Heartbeat: auth OK")
        except subprocess.TimeoutExpired:
            log.warning("Heartbeat: claude auth status timed out")
        except FileNotFoundError:
            log.warning("Heartbeat: claude CLI not found")
        except Exception as e:
            log.warning(f"Heartbeat check failed: {e}")

    async def _notify_recovery(self, message: str):
        """Send recovery notification to Telegram."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": CHAT_ID, "text": f"🔧 Auto-recovery: {message}"},
                )
        except Exception as e:
            log.warning(f"Failed to send recovery notification: {e}")

    async def _ensure_summarizer_agent(self) -> bool:
        """Ensure the hidden Claude Code summarizer session exists."""
        if self._summarizer_ready:
            # Verify session still alive
            r = subprocess.run(
                ["tmux", "has-session", "-t", SUMMARIZER_AGENT_SESSION],
                capture_output=True,
            )
            if r.returncode == 0:
                # Check for 401 auth error and attempt recovery
                if self._check_session_401(SUMMARIZER_AGENT_SESSION):
                    log.warning("Summarizer session has 401 error, attempting re-auth")
                    await self._recover_session_auth(SUMMARIZER_AGENT_SESSION)
                return True
            self._summarizer_ready = False

        # Create hidden session with Claude Code
        try:
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", SUMMARIZER_AGENT_SESSION,
                 "-x", "200", "-y", "50"],
                check=True, capture_output=True,
            )
            cmd = "claude --dangerously-skip-permissions"
            if SUMMARIZER_AGENT_MODEL:
                cmd += f" --model {SUMMARIZER_AGENT_MODEL}"
            subprocess.run(
                ["tmux", "send-keys", "-t", SUMMARIZER_AGENT_SESSION, cmd, "Enter"],
                check=True,
            )
            # Wait for Claude to start (check for prompt)
            for _ in range(15):  # max 15 seconds
                await asyncio.sleep(1)
                content = capture_pane(SUMMARIZER_AGENT_SESSION, lines=10)
                if has_prompt(content):
                    self._summarizer_ready = True
                    log.info("Summarizer agent ready (Tier 2)")
                    return True
            log.warning("Summarizer agent did not reach prompt in time")
            return False
        except Exception as e:
            log.warning(f"Failed to create summarizer agent: {e}")
            return False

    async def _summarize_via_agent(self, user_question: str, raw_output: str) -> str | None:
        """Use hidden Claude Code session to summarize (Tier 2)."""
        if not await self._ensure_summarizer_agent():
            return None

        # Build the prompt for Claude Code
        prompt = (
            f"REFORMAT TASK: Clean up this terminal output for Telegram. "
            f"Do NOT answer the question yourself — just reformat what the other Claude said. "
            f"Keep code blocks, use the same language, be concise. "
            f"Context — user asked: {user_question[:200]}\n\n"
            f"Terminal output to reformat:\n{raw_output[:4000]}"
        )

        try:
            # Snapshot before sending
            pre = capture_pane(SUMMARIZER_AGENT_SESSION, lines=SCROLLBACK_LINES)

            # Send prompt
            send_keys(SUMMARIZER_AGENT_SESSION, prompt)
            send_enter(SUMMARIZER_AGENT_SESSION)

            # Poll for response (max 30s)
            prev_content = ""
            stable_count = 0
            await asyncio.sleep(2)
            elapsed = 2.0

            prompt_count = 0
            while elapsed < 30:
                content = capture_pane(SUMMARIZER_AGENT_SESSION, lines=15)
                cleaned = clean_output(content).strip()

                if cleaned == prev_content:
                    stable_count += 1
                else:
                    stable_count = 0
                prev_content = cleaned

                if is_idle_prompt(content):
                    prompt_count += 1
                else:
                    prompt_count = 0

                if (stable_count >= STABILITY_THRESHOLD and is_idle_prompt(content)) or prompt_count >= 5:
                    break

                await asyncio.sleep(POLL_INTERVAL)
                elapsed += POLL_INTERVAL
            else:
                log.warning("Summarizer agent timed out")
                return None

            # Extract response
            post = capture_pane(SUMMARIZER_AGENT_SESSION, lines=SCROLLBACK_LINES)
            result = self._extract_diff(pre, post)
            return result if result.strip() else None

        except Exception as e:
            log.warning(f"Summarizer agent error: {e}")
            return None

    def _extract_diff(self, pre: str, post: str) -> str:
        """Extract new content added to pane between pre and post snapshots."""
        pre_lines = pre.strip().split("\n")
        post_lines = post.strip().split("\n")

        # Find where post diverges from pre
        common_len = 0
        for i, (a, b) in enumerate(zip(pre_lines, post_lines)):
            if a == b:
                common_len = i + 1
            else:
                break

        new_lines = post_lines[common_len:]
        # Remove trailing prompt lines
        while new_lines and (not new_lines[-1].strip() or "❯" in new_lines[-1]):
            new_lines.pop()

        return clean_output("\n".join(new_lines)).strip()

    async def _summarize(self, user_question: str, raw_output: str) -> str:
        """3-tier summarization: API proxy -> Claude agent -> raw."""
        log.info(f"Summarizing: {len(raw_output)} chars, api_available={self._api_available}, raw_mode={self.raw_mode}")
        if len(raw_output.strip()) < 50:
            log.info("Skipping summarize: too short")
            return raw_output

        # Tier 1: API proxy
        tier1_payload = {
            "model": SUMMARIZER_MODEL,
            "messages": [
                {"role": "system", "content": SUMMARIZE_PROMPT},
                {"role": "user", "content": (
                    f"[REFORMAT TASK] Clean up the following terminal output for Telegram delivery.\n"
                    f"Context — the user asked: \"{user_question}\"\n\n"
                    f"Raw terminal output to reformat:\n```\n{raw_output[:12000]}\n```\n\n"
                    f"Remember: ONLY reformat. Do NOT answer the user's question yourself."
                )},
            ],
            "max_tokens": 4000,
        }
        tier1_headers = {"Authorization": "Bearer not-needed"}
        tier1_url = f"{SUMMARIZER_URL}/chat/completions"

        _tier1_connection_error = False
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(tier1_url, headers=tier1_headers, json=tier1_payload)
                resp.raise_for_status()
                data = resp.json()
                self._api_available = True
                result = data["choices"][0]["message"]["content"]
                log.info(f"Tier 1 (API) success: {len(result)} chars summarized")
                return result
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            log.warning(f"Tier 1 (API) connection error: {e}, attempting proxy recovery...")
            _tier1_connection_error = True
        except Exception as e:
            log.warning(f"Tier 1 (API) failed: {e}, trying Tier 2...")

        # If connection error, try to restart the proxy and retry Tier 1 once
        if _tier1_connection_error:
            recovered = await self._recover_proxy()
            if recovered:
                try:
                    async with httpx.AsyncClient(timeout=60) as client:
                        resp = await client.post(tier1_url, headers=tier1_headers, json=tier1_payload)
                        resp.raise_for_status()
                        data = resp.json()
                        self._api_available = True
                        result = data["choices"][0]["message"]["content"]
                        log.info(f"Tier 1 (API) retry success: {len(result)} chars summarized")
                        return result
                except Exception as e:
                    log.warning(f"Tier 1 (API) retry after recovery failed: {e}, trying Tier 2...")
            else:
                log.warning("Proxy recovery failed, trying Tier 2...")

        # Tier 2: Claude Code agent in hidden tmux
        # Check for 401 before attempting to use the session
        if self._check_session_401(SUMMARIZER_AGENT_SESSION):
            log.warning("Summarizer session shows 401 before Tier 2, attempting re-auth")
            await self._recover_session_auth(SUMMARIZER_AGENT_SESSION)

        result = await self._summarize_via_agent(user_question, raw_output)
        if result:
            return result

        # Tier 3: Raw output
        log.info("All summarizers failed, returning raw output")
        return raw_output

    def _cleanup_summarizer(self):
        """Kill the summarizer tmux session if it exists."""
        if self._summarizer_ready:
            subprocess.run(
                ["tmux", "kill-session", "-t", SUMMARIZER_AGENT_SESSION],
                capture_output=True,
            )
            log.info("Summarizer agent session killed")

    def run(self):
        """Start the bot."""
        async def on_shutdown(app):
            self._cleanup_summarizer()
            # Kill cron tmux sessions
            for job in self._cron_jobs:
                session_name = f"cron-{job['id']}"
                if self._agent_session_alive(session_name):
                    subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)

        async def on_init(app):
            # Cache bot reference for background tasks (e.g. CLI mirror)
            self._bot_ref = app.bot
            if self._cron_jobs:
                self._schedule_cron_jobs(app.job_queue)
                log.info(f"Cron scheduler initialized with {len(self._cron_jobs)} job(s)")
            # Auth heartbeat: check every 30 minutes
            app.job_queue.run_repeating(
                self._heartbeat_check, interval=1800, first=300,
                name="auth-heartbeat",
            )
            log.info("Auth heartbeat scheduled (every 30 min)")
            # Load skills from evolve system
            self._load_skills()
            # Evolve proposal checker: every 30 min, first check at 10 min
            app.job_queue.run_repeating(
                self._check_evolve_proposals, interval=1800, first=600,
                name="evolve-proposal-check",
            )
            log.info("Evolve proposal checker scheduled (every 30 min)")
            # Session recovery: deliver any missed messages from previous session
            await self._recover_pending_messages(app.bot)
            # Start CLI mirror if enabled (config-persisted or env-default)
            if self.mirror_on:
                if self._mirror_task is None or self._mirror_task.done():
                    self._mirror_task = asyncio.create_task(self._mirror_loop())
                    log.info("Mirror loop started (mirror_on=True)")
            # Register Telegram native command menu (overrides OpenClaw pollution)
            await self._register_native_commands(app.bot)
            # Periodic re-registration to resist OpenClaw gateway restarts
            asyncio.create_task(self._native_menu_periodic())
            # Hot-reload watcher (mtime poll every 10s)
            if SKILLS_HOT_RELOAD:
                asyncio.create_task(self._skills_hot_reload_loop())

        app = (
            Application.builder()
            .token(BOT_TOKEN)
            .post_init(on_init)
            .post_shutdown(on_shutdown)
            .read_timeout(30)
            .write_timeout(30)
            .connect_timeout(15)
            .build()
        )
        self._app = app

        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_start))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("target", self.cmd_target))
        app.add_handler(CommandHandler("cancel", self.cmd_cancel))
        app.add_handler(CommandHandler("escape", self.cmd_escape))
        app.add_handler(CommandHandler("raw", self.cmd_raw))
        app.add_handler(CommandHandler("mirror", self.cmd_mirror))
        app.add_handler(CommandHandler("reasoning", self.cmd_reasoning))
        app.add_handler(CommandHandler("model", self.cmd_model))
        app.add_handler(CommandHandler("sessions", self.cmd_sessions))
        app.add_handler(CommandHandler("get", self.cmd_get))
        app.add_handler(CommandHandler("recall", self.cmd_recall))
        app.add_handler(CommandHandler("agents", self.cmd_agents))
        app.add_handler(CommandHandler("agent", self.cmd_agent))
        app.add_handler(CommandHandler("assign", self.cmd_assign))
        app.add_handler(CommandHandler("cron", self.cmd_cron))
        app.add_handler(CallbackQueryHandler(self._handle_callback))
        app.add_handler(CommandHandler("evolve", self.cmd_evolve))
        app.add_handler(CommandHandler("lcskill", self.cmd_lcskill))
        app.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        # Start pipe-pane
        self._start_pipe()

        # Probe Tier 1 API availability
        import asyncio as _aio
        loop = _aio.new_event_loop()
        self._api_available = loop.run_until_complete(self._probe_api())
        if self._api_available:
            loop.close()
            log.info(f"Summarizer: API proxy available at {SUMMARIZER_URL}")
        else:
            log.info("Summarizer: API proxy not available, will use Claude Code agent (Tier 2)")
            loop.run_until_complete(self._ensure_summarizer_agent())
            loop.close()

        # Start dashboard in background thread
        if DASHBOARD_PORT:
            handler = partial(DashboardHandler, self)
            server = HTTPServer(("0.0.0.0", DASHBOARD_PORT), handler)
            dash_thread = threading.Thread(target=server.serve_forever, daemon=True)
            dash_thread.start()
            log.info(f"Dashboard started at http://localhost:{DASHBOARD_PORT}")

        log.info(f"Bridge started. Target: {self.target}")
        log.info("Send a message to your Telegram bot to begin.")
        # Loop 4 will populate `extras` with resume/primer status; for now this
        # ships the basic ready ping so users know startup completed.
        self._send_boot_ready()
        app.run_polling(drop_pending_updates=False)


def _is_binary(path: Path) -> bool:
    """Quick check if a file is likely binary."""
    try:
        chunk = path.read_bytes()[:8192]
        return b"\x00" in chunk
    except OSError:
        return True


# =============================================================================
# Entry
# =============================================================================

def main():
    """Entry point for the liteclaw command."""
    if not BOT_TOKEN or not CHAT_ID:
        print("Error: BOT_TOKEN and CHAT_ID must be set in .env")
        print("  BOT_TOKEN  - get from @BotFather on Telegram (/newbot)")
        print("  CHAT_ID    - get from @userinfobot on Telegram")
        sys.exit(1)
    session_name = TMUX_TARGET.split(":")[0]
    r = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    if r.returncode != 0:
        print(f"Error: tmux session '{session_name}' not found.")
        print("Available sessions:")
        subprocess.run(["tmux", "list-sessions"])
        print(f"\nStart Claude Code first:\n  tmux new-session -s {session_name} 'claude --dangerously-skip-permissions'")
        sys.exit(1)

    claw = LiteClaw()
    claw.run()


if __name__ == "__main__":
    main()
