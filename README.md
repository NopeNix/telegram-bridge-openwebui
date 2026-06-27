<div align="center">

# Telegram Bride for Open WebUI

**Two-way Telegram bridge that proxies conversations through Open WebUI's native chat API.**
Replies on Telegram appear in your real Open WebUI chat history — and vice versa.

[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![python-telegram-bot 21.x](https://img.shields.io/badge/python--telegram--bot-21.x-blue.svg)](https://docs.python-telegram-bot.org/)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](Dockerfile)

[Features](#features) • [How it works](#how-it-works) • [Quick start](#quick-start) • [Configuration](#configuration) • [Commands](#commands) • [Troubleshooting](#troubleshooting)

</div>

---

## Why this exists

Open WebUI has a built-in Telegram *tool* that lets the model push notifications to your phone — perfect for "ping me when you find something". But there's no built-in way to **reply** to those notifications and have the answer flow back into the same chat in Open WebUI.

This bridge fills that gap. It runs as a small sidecar service that:

- Receives your Telegram messages (long polling, no public HTTPS endpoint needed)
- Creates real, persistent chats in Open WebUI via its native `/api/v1/chats/new` endpoint
- Streams the model's reply back to Telegram with a clickable link to the conversation
- Continues the same Open WebUI chat when you reply to one of the bot's messages

The result: Telegram becomes a remote control for Open WebUI, and every chat you have over Telegram shows up in your sidebar with full history, tools, system prompts, and RAG — exactly as if you typed it in the web UI.

## Features

- 🚀 **Zero-friction setup** — single container, ~5 env vars, done
- 🔁 **True two-way sync** — every Telegram reply becomes a user message in your Open WebUI chat history
- 🔗 **Clickable backlinks** — every bot reply includes a `https://your-host/c/<chat-id>` link so you can jump straight back to the web UI
- 🔒 **Per-user allowlist** — lock the bot to your Telegram user ID only
- 🧠 **Model-agnostic** — works with any model your Open WebUI instance has access to (Ollama, OpenAI, Anthropic, custom)
- 🛠️ **Tools pass-through** — Open WebUI tools (web search, knowledge bases, the `send_telegram` notifier you may already have) work in Telegram-initiated chats too
- 🐳 **Tiny footprint** — `python:3.12-slim` base, runs comfortably on 50 MB RAM

## How it works

```
┌────────────┐    updates    ┌─────────────────────┐    REST     ┌──────────────┐
│  Telegram  │ ────────────► │  telegram-bride     │ ──────────► │  Open WebUI  │
│  (your     │               │  (this container)   │             │  /api/v1/... │
│   phone)   │ ◄──────────── │                     │ ◄────────── │              │
└────────────┘   bot reply   └─────────────────────┘  completion  └──────────────┘
                                     │
                                     │ creates / appends to a real chat
                                     ▼
                              Open WebUI chat DB
                              (visible in your sidebar)
```

The bridge translates Telegram updates into two kinds of Open WebUI API calls:

| Telegram event | Open WebUI call |
|---|---|
| New message (or message without a reply) | `POST /api/v1/chats/new` → `POST /api/chat/completions` |
| Reply to one of the bot's messages | `POST /api/chat/completions` (appends to existing chat) |

Because chats are created with Open WebUI's native chat endpoint, they persist with the same metadata as any other chat — model, system prompt, knowledge attachments, tool calls.

## Quick start

### Prerequisites

- A running [Open WebUI](https://github.com/open-webui/open-webui) instance
- A Telegram bot token (get one from [@BotFather](https://t.me/BotFather))
- Your Telegram user ID (get it from [@userinfobot](https://t.me/userinfobot))
- Docker + Docker Compose (or Python 3.12+ if you prefer to run it bare)

### 1. Clone

```bash
git clone https://github.com/nopenix/telegram-bride-openwebui.git
cd telegram-bride-openwebui
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and fill in the required values:

```dotenv
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
OPENWEBUI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
OPENWEBUI_BASE_URL=http://host.docker.internal:3000
ALLOWED_USER_IDS=123456789
```

`OPENWEBUI_BASE_URL` notes:

- If Open WebUI runs **on the host** (not in a container): use `http://host.docker.internal:3000`
- If Open WebUI runs **in a sibling container** on the same Docker network: use the service name, e.g. `http://open-webui:8080`
- If both run on **bare metal**: use `http://localhost:3000`

### 3. Run

```bash
docker compose up -d
```

Watch the logs:

```bash
docker compose logs -f telegram-bridge
```

You should see something like:

```
INFO  owui-telegram: Starting Telegram → Open WebUI bridge
INFO  owui-telegram: Open WebUI: http://host.docker.internal:3000 | default model: (default)
INFO  owui-telegram: Allowed Telegram user IDs: [123456789]
```

### 4. Test

Open Telegram, find your bot, send `/start`. Then send any message — within a few seconds you'll get a reply with a link to your brand new Open WebUI chat.

## Configuration

All configuration is via environment variables (see [`.env.example`](.env.example)).

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from [@BotFather](https://t.me/BotFather) |
| `OPENWEBUI_API_KEY` | ✅ | — | Open WebUI API key (Settings → Account → API Keys) |
| `OPENWEBUI_BASE_URL` | | `http://localhost:3000` | Base URL of your Open WebUI instance |
| `DEFAULT_MODEL` | | (OWUI default) | Model to use for new chats (e.g. `gpt-4o`, `llama3.1:8b`) |
| `ALLOWED_USER_IDS` | ⭐ | (all) | Comma-separated Telegram user IDs. **Strongly recommended** to set. |
| `REQUEST_TIMEOUT` | | `120` | HTTP timeout in seconds for Open WebUI API calls |
| `LOG_LEVEL` | | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### Bare-metal / non-Docker run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)
python telegram_bot.py
```

## Commands

| Command | Description |
|---|---|
| `/start` | Show usage help |
| `/newchat` | Start a fresh chat on the next message |
| `/id` | Show a link to the current chat |
| `/model` | Show the default model in use |

### Conversation flow

```
You:        "What's the best Python ORM in 2025?"
Bot:        "SQLAlchemy 2.0 and SQLModel..."   ← new chat created
            🔗 http://host:3000/c/abc-123

You:        (reply to that message) "Show me a SQLModel example"
Bot:        "Here's a quick example..."         ← same chat continued
            🔗 http://host:3000/c/abc-123

You:        /newchat
Bot:        "🆕 Next message will start a fresh chat."

You:        "Now help me with a regex"
Bot:        "Sure! What are you matching?"      ← new chat, new id
            🔗 http://host:3000/c/def-456
```

## Troubleshooting

### Bot doesn't reply

1. Check `docker compose logs telegram-bridge` for errors.
2. Verify `TELEGRAM_BOT_TOKEN` is correct — `/start` should always respond even if Open WebUI is down.
3. Verify `ALLOWED_USER_IDS` contains your Telegram user ID. If empty, all users are allowed.

### `❌ Open WebUI returned 401`

Wrong or expired `OPENWEBUI_API_KEY`. Generate a new one in Settings → Account → API Keys.

### `❌ Open WebUI returned 403`

The API key's user lacks permission for some operation (usually the chat creation). Make sure the key belongs to an admin or has the required group permissions.

### `❌ Open WebUI returned 404`

`OPENWEBUI_BASE_URL` doesn't resolve from inside the container. Try `http://host.docker.internal:3000` (host deployment) or the Docker service name (sibling container).

### Chats appear in Open WebUI but messages look empty

Open WebUI sometimes needs a moment to persist the assistant message. This is cosmetic — refreshing the chat in the web UI will show the full reply.

### I want to use a different model per chat

Set `DEFAULT_MODEL` to empty and either:

- Configure your Open WebUI default model in Settings → Models, or
- Modify `telegram_bot.py` to add a `/model <name>` command and pass `model` per-call

PRs welcome for the latter.

## Limitations

- **Text only.** Photos, voice messages, and files are not yet handled. Contributions welcome.
- **Single active chat per Telegram user.** Use `/newchat` to switch contexts.
- **Long polling, not webhooks.** Simpler to deploy, but a webhook mode is planned for users who already have a public reverse proxy.
- **No streaming responses.** The bot waits for the full reply before sending to Telegram. Token-by-token streaming is on the roadmap.

## Contributing

Contributions are welcome. Please open an issue first to discuss substantial changes.

```bash
git clone https://github.com/nopenix/telegram-bride-openwebui.git
cd telegram-bride-openwebui
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in your values
python telegram_bot.py
```

## Security

- **Always set `ALLOWED_USER_IDS`.** Without it, anyone who finds your bot can use it.
- The bot reads/writes your Open WebUI data. Treat `OPENWEBUI_API_KEY` like a password.
- The image runs as a non-root user (`uid 1000`) inside the container.
- No data is logged except operational metadata (user IDs, error responses).

## License

[MIT](LICENSE) — do whatever you want, no warranty.

## Acknowledgements

- [Open WebUI](https://github.com/open-webui/open-webui) — the platform this bridges to
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) — the Telegram client library
- Inspired by the gap left by [Sid-Sun/openwebui-telegram](https://github.com/Sid-Sun/openwebui-telegram), which uses the OpenAI-compatible API and therefore doesn't create persistent chats in Open WebUI.

---

<div align="center">

Made with too much coffee and a stubborn refusal to switch apps mid-thought.

</div>