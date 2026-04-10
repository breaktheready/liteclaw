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
import html
import json as _json
import logging
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from functools import partial
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =============================================================================
# Configuration
# =============================================================================

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
TMUX_TARGET = os.environ.get("TMUX_TARGET", "claude:1")

# Summarizer config (local Claude proxy)
SUMMARIZER_URL = os.environ.get("SUMMARIZER_URL", "http://localhost:8080/v1")
SUMMARIZER_MODEL = os.environ.get("SUMMARIZER_MODEL", "claude-haiku-4-5")
SUMMARIZER_AGENT_MODEL = os.environ.get("SUMMARIZER_AGENT_MODEL", "")  # model for Tier 2 agent
SUMMARIZER_AGENT_SESSION = "liteclaw-summarizer"  # hidden tmux session for Tier 2

POLL_INTERVAL = 1.5      # seconds between capture-pane polls
STABILITY_THRESHOLD = 3   # consecutive unchanged polls = response done
MAX_WAIT = 0              # 0 = no timeout (wait indefinitely)
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
    """Send keystrokes to tmux pane. Long text (>500 chars) is saved to a temp
    file and the path is sent instead, to avoid tmux bracketed paste issues."""
    if literal and len(text) > 500:
        # Long text breaks tmux send-keys (shows as "[Pasted text +N lines]")
        # Save to file and tell Claude to read it
        tmp = f"/tmp/liteclaw_input_{int(datetime.now().timestamp())}.txt"
        Path(tmp).write_text(text, encoding="utf-8")
        msg = f"Read this file and follow the instructions inside: {tmp}"
        cmd = ["tmux", "send-keys", "-t", target, "-l", msg]
        subprocess.run(cmd, check=True)
        log.info(f"Long input ({len(text)} chars) saved to {tmp}")
        return
    cmd = ["tmux", "send-keys", "-t", target]
    if literal:
        cmd.append("-l")
    cmd.append(text)
    subprocess.run(cmd, check=True)


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

SUMMARIZE_PROMPT = """You are a concise assistant that reformats Claude Code CLI output for Telegram.

Rules:
- Extract the meaningful response, discard terminal noise (tool calls, file reads, status lines, hook messages)
- Keep code blocks, commands, key decisions, and action items intact
- Use Telegram-friendly Markdown (bold, code blocks, bullet points)
- Respond in the same language as the user's question
- If the output contains an error, highlight it clearly
- For long outputs: summarize into structured sections with headers, don't just truncate
- If the output contains a plan or list: preserve the structure and all items
- Keep it concise but NEVER drop important content — completeness over brevity
- Do NOT add your own commentary — just reformat what Claude said"""




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
        # Multi-agent registry: {name: {session, project, status}}
        self._agents: dict[str, dict] = {}
        self._agents_file = Path(__file__).parent / ".agents.json"
        self._load_agents()

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

    def _record_offset(self):
        """Record current end of pipe log file."""
        if self._log_path and os.path.exists(self._log_path):
            self._log_offset = os.path.getsize(self._log_path)
        else:
            self._log_offset = 0

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
            "/get FILEPATH — download a file\n\n"
            "Multi-Agent:\n"
            "/agents — list all agents\n"
            "/agent new NAME PATH — create agent\n"
            "/agent status — detailed agent status\n"
            "/agent remove NAME — remove agent\n"
            "/assign NAME task — assign task to agent",
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
        self.target = args[0]
        self._start_pipe()
        await update.message.reply_text(
            f"Target changed to: `{self.target}`", parse_mode="Markdown",
        )

    async def cmd_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return
        try:
            send_keys(self.target, "C-c", literal=False)
            self.busy = False
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
        mode = "ON (raw output)" if self.raw_mode else "OFF (summarized)"
        await update.message.reply_text(f"Raw mode: {mode}")

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
            await update.message.reply_text(f"Agent '{name}' removed.")

        else:
            await update.message.reply_text(
                f"Unknown subcommand: {subcmd}\n"
                "Usage: /agent new|status|remove"
            )

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
            cleaned = pane_content.strip()

            if cleaned == prev_content:
                stable_count += 1
            else:
                stable_count = 0
            prev_content = cleaned

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

            # Done: stable AND prompt visible
            if stable_count >= STABILITY_THRESHOLD and has_prompt(pane_content):
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

        if self.busy:
            await update.message.reply_text("⏳ Still processing. Use /cancel to abort.")
            return

        self.busy = True
        self._last_activity = datetime.now().isoformat()
        log.info(f"Received: {user_text[:80]}...")

        try:
            # Ensure pipe-pane is active
            if not self._pipe_active:
                self._start_pipe()

            # Check if Claude is idle or busy BEFORE sending
            pane_snapshot = capture_pane(self.target, lines=15)
            claude_idle = has_prompt(pane_snapshot)

            if not claude_idle:
                status_lines = clean_output(pane_snapshot).strip().split("\n")[-5:]
                status_preview = "\n".join(l for l in status_lines if l.strip())
                await update.message.reply_text(
                    f"⚠️ Claude is currently busy:\n```\n{status_preview}\n```\n\n"
                    "Message queued — will notify when done.",
                    parse_mode="Markdown",
                )

            # Record offset before sending
            self._record_offset()

            # Send to tmux (Claude Code queues input even when busy)
            send_keys(self.target, user_text)
            send_enter(self.target)

            if claude_idle:
                await update.message.reply_text("📤 Sent. Waiting for response...")

            # Poll for response with streaming feedback
            response = await self._poll_response(ctx.bot, user_text)

            # Summarize (unless raw mode)
            if not response.strip():
                await update.message.reply_text("(empty response)")
                return

            if not self.raw_mode:
                await ctx.bot.send_chat_action(chat_id=CHAT_ID, action=ChatAction.TYPING)
                response = await self._summarize(user_text, response)

            # Send back
            chunks = split_message(response)
            for i, chunk in enumerate(chunks):
                if len(chunks) > 1:
                    header = f"[{i+1}/{len(chunks)}]\n"
                else:
                    header = ""
                await update.message.reply_text(f"{header}{chunk}")
                if i < len(chunks) - 1:
                    await asyncio.sleep(0.5)

        except RuntimeError as e:
            await update.message.reply_text(f"Error: {e}")
        except Exception as e:
            log.exception("Unexpected error")
            await update.message.reply_text(f"Unexpected error: {e}")
        finally:
            self.busy = False

    async def _poll_response(self, bot, user_text: str = "") -> str:
        """Poll until response stabilizes. Uses pipe-pane log for output, capture-pane for prompt detection."""
        prev_content = ""
        stable_count = 0
        elapsed = 0.0
        last_typing = 0.0
        last_status = 0.0
        status_interval = INTERMEDIATE_INTERVAL  # starts at 10s, grows to 60s
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

            # Prompt detection via capture-pane (small window, fast)
            pane_content = capture_pane(self.target, lines=15)
            cleaned = pane_content.strip()

            if cleaned == prev_content:
                stable_count += 1
            else:
                stable_count = 0
            prev_content = cleaned

            # Status update at adaptive interval — show last few meaningful lines
            if elapsed - last_status >= status_interval:
                status_capture = capture_pane(self.target, lines=10)
                preview_text = clean_output(status_capture).strip()
                if preview_text:
                    preview_lines = [l for l in preview_text.split("\n") if l.strip()][-5:]
                    preview = "\n".join(preview_lines)
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
                last_status = elapsed
                status_interval = min(status_interval + 5, 60)

            # Done: stable AND prompt visible
            if stable_count >= STABILITY_THRESHOLD and has_prompt(pane_content):
                break

            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
        else:
            log.warning(f"Timeout after {MAX_WAIT}s")

        # Clean up status message
        if status_msg_id:
            try:
                await bot.delete_message(chat_id=CHAT_ID, message_id=status_msg_id)
            except Exception:
                pass

        # Extract response using capture-pane (rendered output, not raw pipe)
        full_capture = capture_pane(self.target, lines=SCROLLBACK_LINES)
        text = self._extract_response(full_capture, user_text)

        if MAX_WAIT > 0 and elapsed >= MAX_WAIT:
            text += "\n\n⚠️ [TIMEOUT — Claude may still be working]"

        log.info(f"Response extracted: {len(text)} chars")
        return text

    def _extract_response(self, capture: str, user_text: str) -> str:
        """Extract Claude's response from capture-pane by finding user echo."""
        cleaned = clean_output(capture)
        lines = cleaned.split("\n")

        search_text = user_text[:50].strip()
        user_echo_idx = -1

        if search_text:
            # Strategy 1: Find line with ❯ prompt + user text (the actual echo line)
            for i in range(len(lines) - 1, -1, -1):
                if "❯" in lines[i] and search_text in lines[i]:
                    user_echo_idx = i
                    break

            # Strategy 2: Find user text on a line immediately after a ❯ line
            if user_echo_idx < 0:
                for i in range(len(lines) - 1, 0, -1):
                    if search_text in lines[i] and "❯" in lines[i - 1]:
                        user_echo_idx = i
                        break

            # Strategy 3: Find user text anywhere (original approach, last resort)
            if user_echo_idx < 0:
                for i in range(len(lines) - 1, -1, -1):
                    if search_text in lines[i]:
                        user_echo_idx = i
                        break

        if user_echo_idx >= 0:
            response_lines = lines[user_echo_idx + 1:]
        else:
            # Fallback: user echo scrolled off. Find the last ❯ prompt pair
            # and take content between the second-to-last ❯ and the final ❯
            log.warning("Could not find user echo in capture-pane, using prompt-pair fallback")
            prompt_indices = [i for i, l in enumerate(lines) if "❯" in l.strip()]
            if len(prompt_indices) >= 2:
                # Take content between second-to-last and last prompt
                start = prompt_indices[-2] + 1
                end = prompt_indices[-1]
                response_lines = lines[start:end]
            elif prompt_indices:
                # Only one prompt (the trailing one) — take everything before it
                response_lines = lines[:prompt_indices[-1]]
            else:
                response_lines = lines

        # Remove trailing prompt (❯) and empty lines
        while response_lines and response_lines[-1].strip().strip("\xa0") in ("❯", ""):
            response_lines.pop()

        # Remove leading empty lines
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
        """Try to restart max-api-proxy Docker container."""
        log.warning("Attempting to restart max-api-proxy...")
        try:
            r = subprocess.run(
                ["docker", "compose", "up", "-d"],
                cwd="/home/breaktheready/projects/max_api_proxy",
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                await asyncio.sleep(3)
                if await self._probe_api():
                    log.info("max-api-proxy recovered successfully")
                    await self._notify_recovery("max-api-proxy restarted")
                    return True
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
        """Send /login to a Claude Code session to re-authenticate."""
        log.warning(f"Attempting to re-authenticate session: {session}")
        try:
            send_keys(session, "/login")
            send_enter(session)
            for _ in range(15):
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
            f"Summarize this Claude Code output for a Telegram message. "
            f"Be concise, keep code blocks, use the same language as the question. "
            f"User asked: {user_question[:200]}\n\n"
            f"Output:\n{raw_output[:4000]}"
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

            while elapsed < 30:
                content = capture_pane(SUMMARIZER_AGENT_SESSION, lines=15)
                cleaned = content.strip()

                if cleaned == prev_content:
                    stable_count += 1
                else:
                    stable_count = 0
                prev_content = cleaned

                if stable_count >= STABILITY_THRESHOLD and has_prompt(content):
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
                    f"User's question:\n{user_question}\n\n"
                    f"Raw Claude Code output:\n```\n{raw_output[:12000]}\n```"
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

        app = (
            Application.builder()
            .token(BOT_TOKEN)
            .post_shutdown(on_shutdown)
            .build()
        )

        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_start))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("target", self.cmd_target))
        app.add_handler(CommandHandler("cancel", self.cmd_cancel))
        app.add_handler(CommandHandler("escape", self.cmd_escape))
        app.add_handler(CommandHandler("raw", self.cmd_raw))
        app.add_handler(CommandHandler("model", self.cmd_model))
        app.add_handler(CommandHandler("sessions", self.cmd_sessions))
        app.add_handler(CommandHandler("get", self.cmd_get))
        app.add_handler(CommandHandler("agents", self.cmd_agents))
        app.add_handler(CommandHandler("agent", self.cmd_agent))
        app.add_handler(CommandHandler("assign", self.cmd_assign))
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
