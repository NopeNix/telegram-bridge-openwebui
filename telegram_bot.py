"""
Open WebUI Telegram Bridge.

Two-way Telegram bot that proxies conversations through Open WebUI's native
chat API. Replies on Telegram appear in your real Open WebUI chat history
and vice versa.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field

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
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENWEBUI_BASE_URL = os.environ.get("OPENWEBUI_BASE_URL", "http://localhost:3000").rstrip("/")
OPENWEBUI_API_KEY = os.environ["OPENWEBUI_API_KEY"]
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "")
ALLOWED_USER_IDS = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "120"))

log = logging.getLogger("owui-telegram")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


# ---------------------------------------------------------------------------
# Per-user session state
# ---------------------------------------------------------------------------
@dataclass
class UserSession:
    chat_id: str | None = None
    last_message_ids: list[str] = field(default_factory=list)


SESSIONS: dict[int, UserSession] = {}


def session_for(user_id: int) -> UserSession:
    if user_id not in SESSIONS:
        SESSIONS[user_id] = UserSession()
    return SESSIONS[user_id]


# ---------------------------------------------------------------------------
# Open WebUI client
# ---------------------------------------------------------------------------
class OpenWebUI:
    def __init__(self, base_url: str, api_key: str) -> None:
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

    async def new_chat(self, model: str, content: str) -> tuple[str, str]:
        """
        Create a chat with a user message and an empty assistant placeholder.
        Returns (chat_id, assistant_message_id).
        """
        chat_id = str(uuid.uuid4())
        user_msg_id = str(uuid.uuid4())
        assistant_msg_id = str(uuid.uuid4())
        now = int(time.time())

        payload = {
            "chat": {
                "id": chat_id,
                "title": content[:50] + ("…" if len(content) > 50 else ""),
                "models": [model] if model else [],
                "params": {},
                "history": {
                    "messages": {
                        user_msg_id: {
                            "id": user_msg_id,
                            "parentId": None,
                            "childrenIds": [assistant_msg_id],
                            "role": "user",
                            "content": content,
                            "timestamp": now,
                        },
                        assistant_msg_id: {
                            "id": assistant_msg_id,
                            "parentId": user_msg_id,
                            "childrenIds": [],
                            "role": "assistant",
                            "content": "",
                            "model": model,
                            "timestamp": now,
                        },
                    },
                    "currentId": assistant_msg_id,
                },
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

    async def complete(self, chat_id: str, model: str, content: str) -> str:
        """
        Append a user message to an existing chat and run the model.
        Returns the full assistant text.
        """
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "chat_id": chat_id,
            "stream": False,
            "metadata": {"chat_id": chat_id},
        }
        r = await self._client.post("/api/chat/completions", json=payload)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]


owui = OpenWebUI(OPENWEBUI_BASE_URL, OPENWEBUI_API_KEY)


# ---------------------------------------------------------------------------
# Authorization helper
# ---------------------------------------------------------------------------
def authorized(func):
    """Silently drop updates from anyone not in ALLOWED_USER_IDS."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        "• Send any message — it starts a new chat.\n"
        "• Reply to one of my messages to continue that chat.\n"
        "• /newchat — start a fresh chat.\n"
        "• /id — show a link to the current chat.\n"
        "• /model — show the default model.\n"
    )


@authorized
async def cmd_newchat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session_for(update.effective_user.id).chat_id = None
    await update.message.reply_text("🆕 Next message will start a fresh chat.")


@authorized
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = session_for(update.effective_user.id).chat_id
    if not chat_id:
        await update.message.reply_text("No active chat yet — send a message to start one.")
        return
    url = f"{OPENWEBUI_BASE_URL}/c/{chat_id}"
    await update.message.reply_text(f"Current chat: {url}", disable_web_page_preview=True)


@authorized
async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    label = DEFAULT_MODEL or "Open WebUI default model"
    await update.message.reply_text(f"Default model: `{label}`", parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Telegram message handler
# ---------------------------------------------------------------------------
def _is_reply_to_bot(update: Update) -> bool:
    msg = update.message
    if not msg or not msg.reply_to_message:
        return False
    sender = msg.reply_to_message.from_user
    return bool(sender and sender.is_bot)


@authorized
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = update.message.text or ""
    session = session_for(user.id)

    # Show "typing…" while the model is working
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    try:
        if session.chat_id and _is_reply_to_bot(update):
            # Continue the existing chat
            chat_id = session.chat_id
            reply = await owui.complete(chat_id, DEFAULT_MODEL, text)
        else:
            # Start a new chat
            chat_id, _ = await owui.new_chat(DEFAULT_MODEL, text)
            session.chat_id = chat_id
            reply = await owui.complete(chat_id, DEFAULT_MODEL, text)
    except httpx.HTTPStatusError as e:
        log.exception("Open WebUI API error")
        snippet = e.response.text[:200] if e.response is not None else str(e)
        await update.message.reply_text(f"❌ Open WebUI returned {e.response.status_code}: {snippet}")
        return
    except Exception as e:
        log.exception("Unhandled error")
        await update.message.reply_text(f"❌ {type(e).__name__}: {e}")
        return

    chat_url = f"{OPENWEBUI_BASE_URL}/c/{chat_id}"
    # Telegram message length cap is 4096 chars — truncate gracefully
    body = reply if len(reply) <= 3500 else reply[:3500] + "\n…(truncated, full reply in Open WebUI)"
    await update.message.reply_text(
        f"{body}\n\n🔗 {chat_url}",
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    if not OPENWEBUI_API_KEY:
        raise SystemExit("OPENWEBUI_API_KEY is required")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("newchat", cmd_newchat))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("Starting Telegram → Open WebUI bridge")
    log.info("Open WebUI: %s | default model: %s", OPENWEBUI_BASE_URL, DEFAULT_MODEL or "(default)")
    log.info("Allowed Telegram user IDs: %s", sorted(ALLOWED_USER_IDS) or "(all — set ALLOWED_USER_IDS!)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    finally:
        asyncio.run(owui.aclose())