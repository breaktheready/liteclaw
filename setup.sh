#!/bin/bash
# LiteClaw Setup Script
# Checks and installs prerequisites, sets up Python venv, configures .env

set -e

echo "=== LiteClaw Setup ==="
echo ""

# 1. Check tmux
if ! command -v tmux &>/dev/null; then
    echo "[!] tmux not found. Installing..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get update && sudo apt-get install -y tmux
    elif command -v brew &>/dev/null; then
        brew install tmux
    elif command -v yum &>/dev/null; then
        sudo yum install -y tmux
    elif command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm tmux
    else
        echo "[ERROR] Cannot auto-install tmux. Please install it manually:"
        echo "  Ubuntu/Debian: sudo apt install tmux"
        echo "  macOS:         brew install tmux"
        echo "  Fedora/RHEL:   sudo yum install tmux"
        echo "  Arch:          sudo pacman -S tmux"
        exit 1
    fi
fi
echo "[OK] tmux $(tmux -V)"

# 2. Find Python 3.10+ (prefer newer versions, fall back through named interpreters)
# On macOS, `python3` often resolves to system 3.9 even when brew python@3.12 is installed,
# because brew's formula only provides versioned binaries (python3.12) without a generic
# python3 symlink. Search for versioned interpreters first.
PYTHON_BIN=""
for bin in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$bin" &>/dev/null; then
        ver=$("$bin" -c "import sys; print(sys.version_info.major*100+sys.version_info.minor)" 2>/dev/null)
        if [ -n "$ver" ] && [ "$ver" -ge 310 ]; then
            PYTHON_BIN=$(command -v "$bin")
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo "[ERROR] Python 3.10+ not found. Please install:"
    echo "  Ubuntu/Debian: sudo apt install python3.12 python3.12-venv"
    echo "  macOS:         brew install python@3.12"
    echo "  Fedora/RHEL:   sudo dnf install python3.12"
    exit 1
fi
PY_VER=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "[OK] Python $PY_VER ($PYTHON_BIN)"

# 3. Check Claude Code CLI
if command -v claude &>/dev/null; then
    echo "[OK] Claude Code CLI found"
else
    echo "[!] Claude Code CLI not found"
    echo "    Install: https://docs.anthropic.com/en/docs/claude-code/overview"
    echo "    (LiteClaw requires Claude Code to be running in tmux)"
fi

# 4. Create venv + install deps
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d ".venv" ]; then
    echo ""
    echo "Creating virtual environment..."
    "$PYTHON_BIN" -m venv .venv
fi
echo "Installing dependencies..."
.venv/bin/pip install -q -r requirements.txt
echo "[OK] Dependencies installed"

# 5. Configure .env
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "=== Configuration Required ==="
    echo ""
    echo "Edit .env with your Telegram bot credentials:"
    echo "  nano .env"
    echo ""
    echo "  BOT_TOKEN  - Get from @BotFather on Telegram (/newbot)"
    echo "  CHAT_ID    - Get from @userinfobot on Telegram"
    echo ""
    echo "See README for detailed setup instructions."
else
    echo "[OK] .env already configured"
fi

# 6. Summarizer (Tier 1) availability check
#
# LiteClaw has a 3-tier summarizer with automatic fallback:
#   Tier 1: external OpenAI-compatible proxy (fastest, cleanest output)
#   Tier 2: hidden Claude Code agent inside tmux (works offline, slower)
#   Tier 3: raw terminal output (no summarization)
#
# The bot is fully functional at any tier, but Tier 1 gives the best UX.
SUMMARIZER_URL=$(grep -E '^\s*SUMMARIZER_URL=' .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs)
SUMMARIZER_URL=${SUMMARIZER_URL:-http://localhost:3456/v1}

echo ""
echo "Checking Tier 1 summarizer at ${SUMMARIZER_URL} ..."
if curl -sf --max-time 3 "${SUMMARIZER_URL%/}/models" >/dev/null 2>&1; then
    echo "[OK] Summarizer reachable"
else
    cat <<EOF
[!] Summarizer not reachable at ${SUMMARIZER_URL}
    LiteClaw will still run using Tier 2 (Claude Code agent) fallback.
    For best response quality, run an OpenAI-compatible endpoint at
    SUMMARIZER_URL. Recommended option — reuse your Claude Max subscription:

      https://github.com/mattschwen/claude-max-api-proxy

    Typical install:
      git clone https://github.com/mattschwen/claude-max-api-proxy.git
      cd claude-max-api-proxy && docker compose up -d

    The proxy defaults to port 3456, which matches LiteClaw's default
    SUMMARIZER_URL. If you change the port, update .env accordingly.

    Or point SUMMARIZER_URL / SUMMARIZER_MODEL in .env at any
    OpenAI-compatible API you already have (OpenAI, Groq, local LLM, etc.).
EOF
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env with your bot token and chat ID (if not done)"
echo "  2. Start Claude Code in tmux:"
echo "     tmux new-session -s claude 'claude --dangerously-skip-permissions'"
echo "  3. Run LiteClaw:"
echo "     .venv/bin/python3 liteclaw.py"
echo ""
echo "For more info: https://github.com/breaktheready/liteclaw"
