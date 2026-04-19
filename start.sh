#!/usr/bin/env bash
# start.sh — bring up Claude Code + LiteClaw together.
# Order matters: LiteClaw injects messages into the tmux 'claude' pane,
# so Claude Code must be running there first.
#
# Usage:
#   bash ~/projects/liteclaw/start.sh           # detached
#   bash ~/projects/liteclaw/start.sh --attach  # detached + attach to claude

set -euo pipefail

REPO="$HOME/projects/liteclaw"
LOG="/tmp/liteclaw_run.log"

# 1) Claude Code in tmux 'claude'
if tmux has-session -t claude 2>/dev/null; then
  echo "✓ tmux 'claude' already running"
else
  echo "→ Starting Claude Code in tmux 'claude'..."
  tmux new-session -d -s claude 'claude --dangerously-skip-permissions'
  # Wait up to 20s for Claude prompt or trust-prompt
  for _ in $(seq 1 20); do
    pane=$(tmux capture-pane -t claude -p 2>/dev/null || true)
    if echo "$pane" | grep -qE "❯|Yes, I trust"; then
      break
    fi
    sleep 1
  done
  if echo "$pane" | grep -q "Yes, I trust"; then
    echo "  ⚠ trust-prompt detected — sending Enter to accept"
    tmux send-keys -t claude Enter
    sleep 2
  fi
  echo "  ready"
fi

# 2) LiteClaw daemon in tmux 'liteclaw'
if pgrep -f "python.*liteclaw.py" >/dev/null; then
  echo "✓ LiteClaw already running (pid $(pgrep -f 'python.*liteclaw.py' | head -1))"
else
  echo "→ Starting LiteClaw daemon..."
  tmux new-session -d -s liteclaw "cd $REPO && .venv/bin/python3 liteclaw.py 2>&1 | tee -a $LOG"
  sleep 4
  pid=$(pgrep -f "python.*liteclaw.py" | head -1 || true)
  if [ -n "$pid" ]; then
    echo "  pid $pid"
  else
    echo "  ✗ failed to start — check $LOG"
    exit 1
  fi
fi

echo
echo "Sessions:"
tmux list-sessions | grep -E "^(claude|liteclaw):"
echo
echo "Logs:    tail -f $LOG"
echo "Attach:  tmux attach -t claude"

# Optional: attach if requested
if [ "${1:-}" = "--attach" ]; then
  exec tmux attach -t claude
fi
