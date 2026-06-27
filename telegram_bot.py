"""
Open WebUI Telegram Bridge (v1.1.0).

Two-way Telegram bot that proxies conversations through Open WebUI's native
chat API. Replies on Telegram appear in your real Open WebUI chat history
and vice versa.

Features:
- Model auto-resolution: env var → user OWUI default → first available
- Streaming responses with throttled message edits
- Multi-message chunking for long replies
- Image (photo) support via OWUI file upload
- Per-chat system prompt (/system)
- Model switching at runtime (/model)
- Persistent sessions across container restarts
- Real HTTP /health endpoint for container orchestration
- Optional webhook mode (set WEBHOOK_URL)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Configuration (environment variables)
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
OPENWEBUI_BASE_URL: str = os.environ.get("OPENWEBUI_BASE_URL", "http://localhost:3000").rstrip("/")
OPENWEBUI_API_KEY: str = os.environ["OPENWEBUI_API_KEY"]
DEFAULT_MODEL: str = os.environ.get("DEFAULT_MODEL", "")
DEFAULT_SYSTEM_PROMPT: str = os.environ.get("DEFAULT_SYSTEM_PROMPT", "")
ALLOWED_USER_IDS: set[int] = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()
}
REQUEST_TIMEOUT: float = float(os.environ.get("REQUEST_TIMEOUT", "120"))
SESSIONS_FILE: str = os.environ.get("SESSIONS_FILE", "/app/data/sessions.json")
WEBHOOK_URL: str = os.environ.get("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_PORT: int = int(os.environ.get("WEBHOOK_PORT", "8080"))
WEBHOOK_LISTEN: str = os.environ.get("WEBHOOK_LISTEN", "0.0.0.0")
HEALTH_PORT: int = int(os.environ.get("HEALTH_PORT", "8088"))
STREAM_THROTTLE: float = float(os.environ.get("STREAM_THROTTLE_SECONDS", "1.0"))
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

log = logging.getLogger("owui-telegram")
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def chunk_message(text: str, max_len: int = 3500) -> list[str]:
    """
    Split long text at paragraph / line / word boundaries.
    Telegram's message cap is 4096 chars; we leave headroom for the backlink.
    """
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n\n", 0, max_len)
        if split_at < max_len // 2:
            split_at = remaining.rfind("\n", 0, max_len)
        if split_at < max_len // 2:
            split_at = remaining.rfind(" ", 0, max_len)
        if split_at < max_len // 2:
            split_at = max_len
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks


# ---------------------------------------------------------------------------
# Open WebUI client
# ---------------------------------------------------------------------------
class OpenWebUI:
    """Async client for Open WebUI's chat & admin APIs."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def healthcheck(self) -> bool:
        try:
            r = await self._client.get("/api/models", timeout=5.0)
            return r.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[dict[str, Any]]:
        """Return available models (handles both flat and wrapped response shapes)."""
        r = await self._client.get("/api/models")
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        if isinstance(data, list):
            return data
        return []

    async def get_user_default_model(self) -> Optional[str]:
        """Fetch the authenticated user's UI default model."""
        try:
            r = await self._client.get("/api/v1/users/user/settings")
            if r.status_code != 200:
                return None
            data = r.json()
            models = data.get("ui", {}).get("models", [])
            if models and models[0]:
                return models[0]
        except Exception as e:
            log.warning("Could not fetch user settings: %s", e)
        return None

    async def new_chat(
        self,
        model: str,
        content: str,
        file_ids: Optional[list[str]] = None,
    ) -> tuple[str, str]:
        """
        Create a chat with a user message and empty assistant placeholder.
        Returns (chat_id, assistant_message_id).
        """
        chat_id = str(uuid.uuid4())
        user_msg_id = str(uuid.uuid4())
        assistant_msg_id = str(uuid.uuid4())
        now = int(time.time())

        user_msg: dict[str, Any] = {
            "id": user_msg_id,
            "parentId": None,
            "childrenIds": [assistant_msg_id],
            "role": "user",
            "content": content,
            "timestamp": now,
        }
        if file_ids:
            user_msg["files"] = [{"type": "file", "id": fid} for fid in file_ids]

        history: dict[str, dict[str, Any]] = {
            user_msg_id: user_msg,
            assistant_msg_id: {
                "id": assistant_msg_id,
                "parentId": user_msg_id,
                "childrenIds": [],
                "role": "assistant",
                "content": "",
                "model": model,
                "timestamp": now,
            },
        }

        payload: dict[str, Any] = {
            "chat": {
                "id": chat_id,
                "title": content[:50] + ("…" if len(content) > 50 else ""),
                "models": [model] if model else [],
                "params": {},
                "files": [{"type": "file", "id": fid} for fid in (file_ids or [])],
                "history": {"messages": history, "currentId": assistant_msg_id},
                "messages": [
                    {"id": user_msg_id, "role": "user", "content": content},
                    {"id": assistant_msg_id, "role": "assistant", "content": ""},
                ],
                "tags": [],
                "timestamp": now,
            },
        }
        r = await self._client.post("/api/v1/chats/new", json=payload)
        r.raise_for_status()
        return chat_id, assistant_msg_id

    async def complete(
        self,
        chat_id: str,
        model: str,
        content: str,
        system_prompt: Optional[str] = None,
        file_ids: Optional[list[str]] = None,
        stream: bool = False,
    ) -> Any:
        """
        Run a completion. If stream=True, returns an async iterator yielding text chunks.
        Otherwise returns the full assistant text.
        """
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": content})

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "chat_id": chat_id,
            "stream": stream,
            "metadata": {"chat_id": chat_id},
        }
        if file_ids:
            payload["files"] = [{"type": "file", "id": fid} for fid in file_ids]

        if stream:
            return self._stream(payload)
        r = await self._client.post("/api/chat/completions", json=payload)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    async def _stream(self, payload: dict[str, Any]) -> AsyncIterator[str]:
        async with self._client.stream("POST", "/api/chat/completions", json=payload) as resp:
            resp.raise_for_status()
            buffer = ""
            async for chunk in resp.aiter_text():
                buffer += chunk
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        obj = json.loads(data)
                        delta = obj["choices"][0].get("delta", {}).get("content", "")
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

    async def upload_file(self, file_bytes: bytes, filename: str) -> str:
        """Upload a binary file to OWUI and return its file id."""
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {OPENWEBUI_API_KEY}"},
            timeout=REQUEST_TIMEOUT,
        ) as c:
            files = {"file": (filename, file_bytes)}
            r = await c.post("/api/v1/files/", files=files)
            r.raise_for_status()
            return r.json()["id"]


owui = OpenWebUI(OPENWEBUI_BASE_URL, OPENWEBUI_API_KEY)


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------
@dataclass
class ResolvedModel:
    name: str
    source: str  # one of: "env", "user-settings", "first-available", "explicit"

    def __str__(self) -> str:
        return f"{self.name} (from {self.source})"


_current_model: Optional[ResolvedModel] = None
_current_model_lock = threading.Lock()


async def resolve_model(specific: Optional[str] = None) -> ResolvedModel:
    """
    Decide which model to use. Priority:
    1. Explicit `specific` arg (from /model command)
    2. DEFAULT_MODEL env var
    3. User's OWUI default (ui.models[0] from /api/v1/users/user/settings)
    4. First available model from /api/models
    """
    if specific:
        models = await owui.list_models()
        valid_ids = {m.get("id") for m in models}
        if specific not in valid_ids:
            log.warning("Model '%s' not in available list (proceeding anyway)", specific)
        return ResolvedModel(specific, "explicit (/model command)")

    if DEFAULT_MODEL:
        return ResolvedModel(DEFAULT_MODEL, "env DEFAULT_MODEL")

    user_default = await owui.get_user_default_model()
    if user_default:
        return ResolvedModel(user_default, "user settings (ui.models[0])")

    models = await owui.list_models()
    if models:
        first_id = models[0].get("id")
        if first_id:
            return ResolvedModel(first_id, "first from /api/models")

    raise RuntimeError(
        "No model could be resolved. Set DEFAULT_MODEL in .env, or configure "
        "a default model in Open WebUI user settings, or expose at least one model."
    )


def current_model() -> ResolvedModel:
    if _current_model is None:
        raise RuntimeError("Model not yet resolved; should be set in post_init()")
    return _current_model


def set_current_model(model: ResolvedModel) -> None:
    global _current_model
    with _current_model_lock:
        _current_model = model


# ---------------------------------------------------------------------------
# Persistent session store
# ---------------------------------------------------------------------------
@dataclass
class UserSession:
    chat_id: Optional[str] = None
    system_prompt: Optional[str] = None


class SessionStore:
    """JSON-backed per-user session storage with atomic writes."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except Exception as e:
                log.warning("Could not load sessions from %s: %s", self.path, e)
        return {}

    def _save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            tmp.replace(self.path)
        except Exception as e:
            log.warning("Could not save sessions: %s", e)

    def get(self, user_id: int) -> UserSession:
        with self._lock:
            entry = self._data.setdefault(str(user_id), {})
            return UserSession(
                chat_id=entry.get("chat_id"),
                system_prompt=entry.get("system_prompt") or DEFAULT_SYSTEM_PROMPT or None,
            )

    def update(self, user_id: int, *, chat_id: Optional[str] = None,
               system_prompt: Optional[str] = None,
               clear_system_prompt: bool = False) -> UserSession:
        with self._lock:
            entry = self._data.setdefault(str(user_id), {})
            if chat_id is not None:
                entry["chat_id"] = chat_id
            if clear_system_prompt:
                entry["system_prompt"] = None
            elif system_prompt is not None:
                entry["system_prompt"] = system_prompt
            self._save()
            return UserSession(
                chat_id=entry.get("chat_id"),
                system_prompt=entry.get("system_prompt"),
            )


sessions = SessionStore(SESSIONS_FILE)


# ---------------------------------------------------------------------------
# HTTP health server
# ---------------------------------------------------------------------------
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path == "/health":
            ready = _current_model is not None
            self.send_response(200 if ready else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            body = {
                "status": "ok" if ready else "starting",
                "model": str(_current_model) if _current_model else None,
            }
            self.wfile.write(json.dumps(body).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence the default request logger; we have our own.
        return


def start_health_server(port: int) -> None:
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(
        target=server.serve_forever, daemon=True, name="health-server"
    )
    thread.start()
    log.info("Health endpoint listening on :%d/health", port)


# ---------------------------------------------------------------------------
# Authorization decorator
# ---------------------------------------------------------------------------
def authorized(func: Any) -> Any:
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        user = update.effective_user
        if ALLOWED_USER_IDS and user.id not in ALLOWED_USER_IDS:
            log.warning("Rejected unauthorized user id=%s username=%s", user.id, user.username)
            return
        return await func(update, context)
    return wrapper


# ---------------------------------------------------------------------------
# Telegram command handlers
# ---------------------------------------------------------------------------
@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Connected to your Open WebUI.\n\n"
        "• Send any message — starts a new chat.\n"
        "• Reply to one of my messages — continues that chat.\n"
        "• Send a photo with caption — image gets attached to a new chat.\n"
        "• /newchat — start a fresh chat.\n"
        "• /id — show a link to the current chat.\n"
        "• /model [name] — show or switch the active model.\n"
        "• /system [show|clear|<prompt>] — manage per-chat system prompt.\n"
    )


@authorized
async def cmd_newchat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sessions.update(update.effective_user.id, chat_id=None)
    await update.message.reply_text("🆕 Next message will start a fresh chat.")


@authorized
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = sessions.get(update.effective_user.id)
    if not s.chat_id:
        await update.message.reply_text("No active chat yet — send a message to start one.")
        return
    url = f"{OPENWEBUI_BASE_URL}/c/{s.chat_id}"
    await update.message.reply_text(f"Current chat: {url}", disable_web_page_preview=True)


@authorized
async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []

    if not args:
        try:
            models = await owui.list_models()
        except Exception as e:
            await update.message.reply_text(f"❌ Could not list models: {e}")
            return
        model_lines = "\n".join(f"  • `{m.get('id')}`" for m in models[:25])
        await update.message.reply_text(
            f"Current model: `{current_model().name}`\n"
            f"Source: {current_model().source}\n\n"
            f"Available models:\n{model_lines}\n\n"
            f"Use `/model <name>` to switch.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    requested = args[0]
    try:
        new_model = await resolve_model(specific=requested)
        set_current_model(new_model)
        await update.message.reply_text(
            f"✅ Switched to: `{new_model.name}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Could not switch model: {e}")


@authorized
async def cmd_system(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    user_id = update.effective_user.id
    s = sessions.get(user_id)

    if not args or args[0].lower() == "show":
        prompt = s.system_prompt or "(none — using OWUI default behavior)"
        await update.message.reply_text(f"Current system prompt:\n\n{prompt}")
        return

    if args[0].lower() in ("clear", "reset", "none"):
        sessions.update(user_id, clear_system_prompt=True)
        await update.message.reply_text("✅ System prompt cleared.")
        return

    prompt = " ".join(args)
    sessions.update(user_id, system_prompt=prompt)
    await update.message.reply_text(
        f"✅ System prompt set. It will be applied to your next message:\n\n{prompt}"
    )


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------
def _is_reply_to_bot(update: Update) -> bool:
    msg = update.message
    if not msg or not msg.reply_to_message:
        return False
    sender = msg.reply_to_message.from_user
    return bool(sender and sender.is_bot)


async def _stream_reply_to_telegram(
    update: Update, chat_id: str, stream: AsyncIterator[str]
) -> None:
    """Edit a placeholder message as tokens arrive, then chunk if needed."""
    placeholder = await update.message.reply_text("…")
    accumulated = ""
    last_edit = 0.0

    async for chunk in stream:
        accumulated += chunk
        now = time.monotonic()
        if now - last_edit >= STREAM_THROTTLE:
            preview = accumulated if len(accumulated) <= 4000 else accumulated[-4000:]
            try:
                await placeholder.edit_text(preview + " …")
            except Exception:
                pass
            last_edit = now

    # Finalize: chunk the full reply, edit placeholder with first chunk,
    # send remaining chunks as new messages, append backlink at the end.
    chunks = chunk_message(accumulated)
    try:
        await placeholder.edit_text(chunks[0])
    except Exception:
        pass
    for extra in chunks[1:]:
        await update.message.reply_text(extra)

    chat_url = f"{OPENWEBUI_BASE_URL}/c/{chat_id}"
    await update.message.reply_text(f"🔗 {chat_url}", disable_web_page_preview=True)


@authorized
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = update.message.text or ""
    if not text:
        return

    session = sessions.get(user.id)
    model = current_model()

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    try:
        if session.chat_id and _is_reply_to_bot(update):
            chat_id = session.chat_id
            stream = await owui.complete(
                chat_id, model.name, text,
                system_prompt=session.system_prompt,
                stream=True,
            )
        else:
            chat_id, _ = await owui.new_chat(model.name, text)
            sessions.update(user.id, chat_id=chat_id)
            stream = await owui.complete(
                chat_id, model.name, text,
                system_prompt=session.system_prompt,
                stream=True,
            )
    except httpx.HTTPStatusError as e:
        log.exception("OWUI API error")
        snippet = (e.response.text[:300] if e.response is not None else str(e))
        await update.message.reply_text(
            f"❌ Open WebUI returned `{e.response.status_code}`\n"
            f"Model: `{model.name}`\n"
            f"Response: `{snippet}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    except Exception as e:
        log.exception("Unhandled error")
        await update.message.reply_text(
            f"❌ {type(e).__name__}: {e}\nModel: `{model.name}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await _stream_reply_to_telegram(update, chat_id, stream)


@authorized
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download the largest photo, upload to OWUI, attach to a new chat."""
    user = update.effective_user
    caption = update.message.caption or "What's in this image?"
    model = current_model()
    session = sessions.get(user.id)

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    try:
        photo = update.message.photo[-1]  # largest size
        tg_file = await context.bot.get_file(photo.file_id)
        file_bytes = bytes(await tg_file.download_as_bytearray())
        file_id = await owui.upload_file(file_bytes, f"tg-{photo.file_unique_id}.jpg")
        log.info("Uploaded photo, OWUI file_id=%s (%d bytes)", file_id, len(file_bytes))

        chat_id, _ = await owui.new_chat(
            model.name, caption, file_ids=[file_id],
        )
        sessions.update(user.id, chat_id=chat_id)

        stream = await owui.complete(
            chat_id, model.name, caption,
            system_prompt=session.system_prompt,
            file_ids=[file_id],
            stream=True,
        )
        await _stream_reply_to_telegram(update, chat_id, stream)
    except Exception as e:
        log.exception("Photo handling failed")
        await update.message.reply_text(
            f"❌ Failed to process image: {type(e).__name__}: {e}",
        )


# ---------------------------------------------------------------------------
# Lifecycle hooks & entry point
# ---------------------------------------------------------------------------
async def post_init(app: Application) -> None:
    """Resolve the model before the bot starts processing messages."""
    log.info("Resolving model (env → user settings → first available)…")
    try:
        resolved = await resolve_model()
        set_current_model(resolved)
        log.info("✓ Model resolved: %s", resolved)
    except Exception as e:
        log.error("Model resolution failed: %s", e)
        log.error(
            "Set DEFAULT_MODEL in .env, or configure a default model in "
            "Open WebUI user settings (Settings → Interface → Default Model)."
        )
        raise


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    if not OPENWEBUI_API_KEY:
        raise SystemExit("OPENWEBUI_API_KEY is required")

    log.info("Starting Telegram → Open WebUI bridge (v1.1.0)")
    log.info("Open WebUI:    %s", OPENWEBUI_BASE_URL)
    log.info("Sessions file: %s", SESSIONS_FILE)
    log.info(
        "Allowed users: %s",
        sorted(ALLOWED_USER_IDS) or "(all — set ALLOWED_USER_IDS!)",
    )

    start_health_server(HEALTH_PORT)

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Register PHOTO before TEXT so photos-with-caption go to on_photo
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("newchat", cmd_newchat))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("system", cmd_system))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    if WEBHOOK_URL:
        log.info("Mode: webhook (%s)", WEBHOOK_URL)
        app.run_webhook(
            listen=WEBHOOK_LISTEN,
            port=WEBHOOK_PORT,
            url_path=TELEGRAM_BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_BOT_TOKEN}",
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        log.info("Mode: polling (set WEBHOOK_URL to switch)")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    finally:
        asyncio.run(owui.aclose())