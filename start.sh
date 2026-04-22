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
# Where claude is launched. Pinned so the LiteClaw-owned session always lives
# under ~/.claude/projects/<encoded-cwd>/<liteclaw_session_id>.jsonl, regardless
# of where the user launches `liteclaw start` from.
CLAUDE_CWD="${CLAUDE_CWD:-$HOME}"
LITECLAW_DIR="${LITECLAW_DIR:-$HOME/.liteclaw}"
SESSIONS_JSON="$LITECLAW_DIR/sessions.json"

# Resolve a stable LiteClaw-owned session UUID. We pre-allocate a UUID and pass
# it via `--session-id` so `liteclaw start` always resumes the same conversation
# even when the user has other Claude Code windows running in the same cwd.
mkdir -p "$LITECLAW_DIR"
SESS=""
if [ -f "$SESSIONS_JSON" ]; then
  SESS=$(python3 -c "
import json, sys
try:
    print(json.load(open('$SESSIONS_JSON')).get('liteclaw_session_id', ''))
except Exception:
    print('')
" 2>/dev/null || true)
fi
if [ -z "$SESS" ]; then
  # Migration: if this cwd already has exactly one Claude Code session jsonl,
  # adopt its UUID so existing conversations carry over. Otherwise allocate fresh.
  ENC=$(echo "$CLAUDE_CWD" | sed 's|/|-|g')
  PROJ_DIR="$HOME/.claude/projects/$ENC"
  ADOPT=""
  if [ -d "$PROJ_DIR" ]; then
    # Pick the most recently modified jsonl (user's active session wins in ties).
    ADOPT=$(ls -t "$PROJ_DIR"/*.jsonl 2>/dev/null | head -1 | xargs -n1 basename 2>/dev/null | sed 's/\.jsonl$//')
  fi
  if [ -n "$ADOPT" ]; then
    SESS="$ADOPT"
    note="adopted existing session"
  else
    SESS=$(uuidgen | tr '[:upper:]' '[:lower:]')
    note="allocated new session"
  fi
  python3 - <<PY
import json, os
p = "$SESSIONS_JSON"
data = {}
if os.path.exists(p):
    try:
        data = json.load(open(p))
    except Exception:
        data = {}
data["liteclaw_session_id"] = "$SESS"
data["created_by"] = "start.sh"
with open(p, "w") as f:
    json.dump(data, f, indent=2)
PY
  echo "→ $note: $SESS"
fi

# 1) Claude Code in tmux 'claude'
if tmux has-session -t claude 2>/dev/null; then
  echo "✓ tmux 'claude' already running"
else
  echo "→ Starting Claude Code in tmux 'claude' (cwd=$CLAUDE_CWD, session=$SESS)..."
  tmux new-session -d -s claude -c "$CLAUDE_CWD" "claude --session-id $SESS --dangerously-skip-permissions"
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
