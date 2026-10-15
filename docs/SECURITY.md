# Secrets — how a leaked key stops being a problem

## What the bot does now (v2.2)

| Layer | What it does |
|---|---|
| **Secrets live outside the repo** | `config.py` reads `$BOT_ENV_FILE`, then `~/.boss-bot/.env` (`%USERPROFILE%\.boss-bot\.env` on Windows), then the legacy `./.env`. Real environment variables beat any file. `git add .` cannot pick up a file that is not in the repo. |
| **No secret has a default in code** | `MT5_PASSWORD`, `TELEGRAM_TOKEN`, `AI_API_KEYS` default to empty. The bot refuses to start without them rather than falling back to a baked-in value (that fallback was the original leak). |
| **Commit-time scanner** | `.githooks/pre-commit` runs `tools/scan_secrets.py --staged` and blocks a commit that adds a Telegram token, `sk-…`/`gsk-…`/Google/AWS/GitHub key, a bearer header, a private key block, or `SECRET_NAME = "literal"`. Enabled by `install.bat`, or once per clone: `git config core.hooksPath .githooks`. Excerpts are masked, never printed. |
| **Log redaction** | `core/secret_guard.py` masks every loaded secret value and every secret-shaped string on every log handler. A stray `f"{exception}"` that contains the Telegram URL no longer reaches `logs/bot.log`. |
| **Non-blocking, token-safe Telegram** | Alerts are queued and sent by a background thread; failures log the exception *type* only. |
| **Preflight** | `preflight.py` reports whether the secrets file is outside the repo. |

Audit the whole repo at any time: `python tools/scan_secrets.py --all`

## What no code can do — do this once

The first commits of this repository (`93b0348`, and everything before `850119b`) contain a **real MT5 account number + password and a Telegram bot token**. Removing them from the latest files does not remove them from history, and the repository is public.

1. **MT5:** change the account password in the broker portal / MT5 (*Tools → Options → Server → Change password*). Treat the account as exposed until you have.
2. **Telegram:** `@BotFather → /revoke` (or `/token`) to issue a new token; put the new one in your secrets file.
3. **AI key (OpenAI/Groq/…):** create a *project key* with a hard monthly spend limit; rotate it if it was ever pasted anywhere.
4. Optionally rewrite history (`git filter-repo --invert-paths` / BFG) — but only *after* rotating; anything already cloned or cached keeps the old values.

## Make a future leak cheap (blast-radius limits)

- Run the bot on its **own broker account** holding only what you can afford to lose; never reuse a password from another service.
- Give the AI provider key a **spend cap** and, where offered, restrict it to the one model in use.
- The Telegram bot should only message **your** chat; if the token leaks, the worst case is spam to you, and revoking takes ten seconds.
- Keep `.env` out of cloud-synced folders (OneDrive/Dropbox) and out of screenshots.
