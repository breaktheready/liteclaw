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

# 2. Check Python 3.10+
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] Python 3 not found. Please install Python 3.10 or higher."
    echo "  Ubuntu/Debian: sudo apt install python3 python3-venv"
    echo "  macOS:         brew install python3"
    exit 1
fi
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
if [ "$PY_MINOR" -lt 10 ]; then
    echo "[ERROR] Python 3.10+ required, found Python $PY_VER"
    exit 1
fi
echo "[OK] Python $PY_VER"

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
    python3 -m venv .venv
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
