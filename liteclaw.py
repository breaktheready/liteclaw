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
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
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

POLL_INTERVAL = 1.5      # seconds between capture-pane polls
STABILITY_THRESHOLD = 3   # consecutive unchanged polls = response done
MAX_WAIT = 0              # 0 = no timeout (wait indefinitely)
SCROLLBACK_LINES = int(os.environ.get("SCROLLBACK_LINES", "500"))
TG_MAX_LEN = 4000         # telegram message length (leave buffer from 4096)

# pipe-pane log directory
PIPE_LOG_DIR = os.environ.get("PIPE_LOG_DIR", "/tmp")

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
    """Send keystrokes to tmux pane."""
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
- Extract the meaningful response, discard terminal noise (tool calls, file reads, status lines)
- Keep code blocks, commands, and key decisions intact
- Use Telegram-friendly Markdown (bold, code blocks, bullet points)
- Respond in the same language as the user's question
- If the output contains an error, highlight it clearly
- Keep it concise but don't lose important details
- Do NOT add your own commentary — just reformat what Claude said"""


async def summarize_response(user_question: str, raw_output: str) -> str:
    """Use Haiku to clean up raw tmux output into a readable Telegram message."""
    if len(raw_output.strip()) < 50:
        return raw_output

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{SUMMARIZER_URL}/chat/completions",
                headers={"Authorization": "Bearer not-needed"},
                json={
                    "model": SUMMARIZER_MODEL,
                    "messages": [
                        {"role": "system", "content": SUMMARIZE_PROMPT},
                        {"role": "user", "content": (
                            f"User's question:\n{user_question}\n\n"
                            f"Raw Claude Code output:\n```\n{raw_output[:6000]}\n```"
                        )},
                    ],
                    "max_tokens": 2000,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        log.warning(f"Summarizer failed: {e}, sending raw output")
        return raw_output


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
        mode = "raw" if self.raw_mode else f"summarized ({SUMMARIZER_MODEL})"
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
            "/get FILEPATH — download a file",
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
                    response = await summarize_response(caption or doc.file_name, response)
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
                    response = await summarize_response(caption or "photo", response)
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
                response = await summarize_response(user_text, response)

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

    def run(self):
        """Start the bot."""
        app = Application.builder().token(BOT_TOKEN).build()

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
        app.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        # Start pipe-pane
        self._start_pipe()

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
