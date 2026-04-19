# macOS Operations Guide

Platform-specific guidance for running LiteClaw + claude-max-api-proxy on macOS.

---

## Why Not Docker for the Proxy on macOS

Docker containers on macOS **cannot access the user keychain**, where the `claude` CLI stores OAuth tokens. The container falls back to reading `~/.claude/.credentials.json` mounted read-only, which goes stale within hours as the host CLI refreshes tokens.

**Symptoms** when Docker is used on macOS:
- `/v1/models` returns `{"data":[]}`
- `/health` shows rising `consecutiveAuthFailures`
- Chat requests fail with `401 Invalid authentication credentials`
- LiteClaw silently degrades to Tier 2 (slow agent-session summarizer)

**Correct setup**: run the proxy natively as a LaunchAgent under your user account so the embedded `claude` CLI can read live keychain credentials. The proxy repo ships an authoritative guide at `claude-max-api-proxy/docs/macos-setup.md`.

---

## Quick Setup

```bash
# 1. Build the proxy from source (Node.js ≥ 22 required)
cd ~/projects/claude-max-api-proxy
npm install
npm run build

# 2. Authenticate Claude CLI on the host (writes to keychain)
claude auth login

# 3. Install the LaunchAgent (follow proxy's docs/macos-setup.md)
#    — generates the plist with paths resolved from $(pwd) and $(command -v node)
#    — bootstraps with launchctl bootstrap "gui/$(id -u)" ...

# 4. Verify
curl -s http://127.0.0.1:3456/health | python3 -m json.tool
curl -s http://127.0.0.1:3456/v1/models
```

After this, both the `auto-recovery` path inside `liteclaw.py` (kicks the LaunchAgent on Tier 1 failure) and `start.sh` (the launcher) will detect macOS and behave correctly.

---

## Management

```bash
# Status
launchctl print "gui/$(id -u)/com.claude-max-api-proxy" | grep -E '(state|pid|runs)'

# Restart (picks up new build)
launchctl kickstart -k "gui/$(id -u)/com.claude-max-api-proxy"

# Stop
launchctl bootout "gui/$(id -u)/com.claude-max-api-proxy"

# Logs
tail -f ~/Library/Logs/claude-max-api-proxy.log
tail -f ~/Library/Logs/claude-max-api-proxy.err.log
```

---

## Launching LiteClaw + Claude Code together

```bash
bash ~/projects/liteclaw/start.sh           # detached
bash ~/projects/liteclaw/start.sh --attach  # detached + attach to claude pane
```

`start.sh` is idempotent (re-running is a no-op when already up), starts the tmux `claude` pane first, waits for the prompt (handling the "trust this folder" dialog automatically), then starts LiteClaw so it has somewhere to inject messages.

---

## Troubleshooting

### Symptom: LiteClaw summaries are slow, Tier 2 doing the work
Tier 1 proxy is down or returning errors. Check:
```bash
curl -s http://127.0.0.1:3456/health \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('auth:', d['auth']['loggedIn'], 'models:', d['models']['available'])"
```
If `auth.loggedIn=false` or `models=[]`: confirm `claude auth status` works on the host. If host is fine but proxy still fails, the deployment likely reverted to Docker — switch back to LaunchAgent.

### Symptom: `localhost` works in the browser/curl but LiteClaw still gets 503/401
A ghost Docker container or other process may be listening on `:3456` over IPv6 while the LaunchAgent listens on IPv4. macOS resolves `localhost` to `::1` first.
```bash
lsof -iTCP:3456 -sTCP:LISTEN          # expect a single 'node' process
docker ps | grep claude-max           # remove any container holding the port
docker stop claude-max-proxy && docker rm claude-max-proxy
```
Belt-and-suspenders: set `SUMMARIZER_URL=http://127.0.0.1:3456/v1` in `.env` to force IPv4.

### Symptom: `launchctl print` shows a high `runs` count
Port conflict or repeated crash. Investigate logs and free the port:
```bash
lsof -iTCP:3456 -sTCP:LISTEN
launchctl bootout "gui/$(id -u)/com.claude-max-api-proxy"
pkill -f 'claude-max-api-proxy/dist/server/standalone.js'
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.claude-max-api-proxy.plist"
```

### Symptom: `~/.claude/.credentials.json` shows expired `expiresAt`
Normal on macOS — the CLI uses keychain; the file is a legacy artifact. Don't manually edit it.
