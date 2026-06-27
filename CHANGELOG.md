# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-06-27

### Added
- **Auto model resolution**: env var → OWUI user default (`/api/v1/users/user/settings` → `ui.models[0]`) → first available from `/api/models`. Fails loudly at startup if none can be resolved.
- **Streaming responses**: replies stream into Telegram via `editMessageText`, throttled by `STREAM_THROTTLE_SECONDS` to respect rate limits.
- **Multi-message chunking**: replies over 3500 chars are split at paragraph / line / word boundaries.
- **Image (photo) support**: photos uploaded to OWUI via `/api/v1/files/`, attached to a new chat with caption as the user prompt.
- **`/model [name]`** command: lists available models and switches the active model mid-session.
- **`/system <show|clear|prompt>`** command: per-chat system prompt that persists across sessions.
- **Persistent session storage**: JSON file at `SESSIONS_FILE` (default `/app/data/sessions.json`), mounted via `./data:/app/data` in docker-compose.
- **HTTP `/health` endpoint** on `HEALTH_PORT` (default 8088), returns `200` once the bot is fully initialized with the resolved model name.
- **Webhook mode**: when `WEBHOOK_URL` is set, switches from long polling to Telegram webhooks.
- **Real OWUI healthcheck** in `docker-compose.yml`: probes `/api/models` rather than just checking env-var presence.
- **Better error messages**: Telegram error replies now include the model name and the first 300 chars of the OWUI error response body.

### Changed
- `docker-compose.yml`: added persistent `./data` volume mount, exposed `/health` port, image tag now versioned (`v1.1.0`).
- `Dockerfile`: pre-creates `/app/data` and hands ownership to the unprivileged `bot` user.
- `.env.example`: rewritten with grouped sections and inline guidance.

## [1.0.0] - 2026-06-27

### Added
- Initial release.
- Long-polling Telegram bridge using `python-telegram-bot` 21.x.
- Native Open WebUI chat creation via `/api/v1/chats/new` so chats persist in the user's sidebar.
- Per-user session bookkeeping (in-memory only).
- `/start`, `/newchat`, `/id`, `/model` commands.
- Allowlist via `ALLOWED_USER_IDS`.

[1.1.0]: #110---2026-06-27
[1.0.0]: #100---2026-06-27