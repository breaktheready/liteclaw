# Contributing to LiteClaw

Thanks for considering a contribution. LiteClaw is a small, opinionated
single-file Python bridge — changes should match that shape.

## Ground rules

- **Keep the core a single file.** `liteclaw.py` intentionally ships as one
  file so operators can read / fork / deploy with zero ceremony. Factor into
  separate modules only when the piece is genuinely standalone (e.g., a new
  executable under `bin/` or an isolated helper).
- **No new Anthropic API key.** The project's reason-for-being is "reuse
  your existing Claude Code CLI session." A patch that introduces a hard
  dependency on `ANTHROPIC_API_KEY` or a specific vendor SDK will be closed.
- **Fallbacks are non-negotiable.** Anything that relies on an external
  service (summarizer proxy, jsonl path, pyannote, etc.) must degrade
  gracefully, not crash, when the external piece is unavailable.
- **Prefer environment variables over new CLI flags.** LiteClaw is
  configured via `.env`. New knobs should land in `.env.example` with a
  one-line description and a sane default.

## Project structure

```
liteclaw/
├── liteclaw.py              # core daemon (~5k lines, single file)
├── start.sh                 # tmux session + session-id pin
├── setup.sh                 # one-shot install (deps, venv, CLI symlink)
├── bin/liteclaw             # global CLI dispatcher
├── .env.example             # all configuration knobs
├── README.md / README_KO.md # user docs
├── MAC-OPS.md               # macOS proxy/launchd notes
└── DEVNOTES.md              # NOT shipped — session-scoped devnotes (gitignored)
```

## Local sanity checks

Before opening a PR run at least:

```bash
python3 -c "import ast; ast.parse(open('liteclaw.py').read()); print('OK')"
bash -n setup.sh start.sh bin/liteclaw
```

The GitHub Actions workflow `.github/workflows/ci.yml` runs the same checks
plus a dependency import test on every push / PR.

## Commit style

- Small, logical commits. Split a feature from refactors from docs.
- Title prefix: `[feat]`, `[fix]`, `[docs]`, `[chore]`, `[refactor]`.
- Body: wrap at ~80 chars, explain the **why** and cite line numbers for
  non-obvious changes.

## Telegram-side behavior changes

If your change affects what ends up in the user's Telegram chat (summarizer
prompt, follow-up edits, jsonl path, etc.), include a short "user-visible
diff" section in the PR description — ideally a before/after screenshot or
transcript snippet. Regressions in Telegram UX have been subtle and hard to
notice in code review alone.

## Issues and discussion

- Bug report → [template](.github/ISSUE_TEMPLATE/bug_report.md). Please
  include `/tmp/liteclaw_run.log` excerpts (redact `BOT_TOKEN`).
- Feature request → [template](.github/ISSUE_TEMPLATE/feature_request.md).
  Concrete proposals move faster than abstract wishes.

## Security

If you find a security issue (credential leak, RCE surface, etc.), please
do NOT open a public issue. Contact the author directly via GitHub
(@breaktheready) with details.

## Release flow

Maintainer-only:

1. Merge PRs into `main`.
2. Tag `vX.Y.Z` on `main`.
3. GitHub Release with notes summarizing the "What's new in vX.Y" section
   from the README.

Thanks for making LiteClaw sharper.
