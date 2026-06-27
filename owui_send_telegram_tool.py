"""
title: Telegram Notifier (bridged)
author: nopenix
description: >
  Send outbound notifications to Telegram via the telegram-bridge-openwebui
  sidecar (recommended), with a fallback to direct Telegram Bot API calls.
  When the bridge is used, replies on Telegram continue the originating
  Open WebUI chat in both directions.
required_open_webui_version: 0.4.0
version: 2.0.0
license: MIT
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests
from pydantic import BaseModel, Field

# Module-level logger so failures show up in Open WebUI logs
log = logging.getLogger(__name__)


class Tools:
    # ------------------------------------------------------------------
    # Valves — admin-configurable per-tool settings
    # ------------------------------------------------------------------
    class Valves(BaseModel):
        # --- Bridge mode (recommended) ---
        bridge_url: str = Field(
            default="http://host.docker.internal:8089",
            description=(
                "URL of the telegram-bridge-openwebui sidecar's /api/outbound endpoint. "
                "When set, the bridge handles Telegram sending and remembers the message "
                "mapping so replies continue the originating Open WebUI chat. "
                "Leave empty to fall back to direct Telegram Bot API calls (no continuity)."
            ),
        )
        bridge_token: str = Field(
            default="",
            description=(
                "Bearer token matching BRIDGE_OUTBOUND_TOKEN on the bridge. "
                "Leave empty if the bridge doesn't require auth."
            ),
            json_schema_extra={"input": {"type": "password"}},
        )

        # --- Direct mode fallback (only used if bridge_url is empty) ---
        bot_token: str = Field(
            default="",
            description=(
                "Telegram bot token from @BotFather. "
                "ONLY used when bridge_url is empty (direct mode)."
            ),
            json_schema_extra={"input": {"type": "password"}},
        )
        chat_id: str = Field(
            default="",
            description=(
                "Your personal Telegram chat_id (the number, can be negative for groups). "
                "ONLY used when bridge_url is empty (direct mode)."
            ),
        )

        # --- Shared ---
        openwebui_url: str = Field(
            default="http://localhost:3000",
            description=(
                "Base URL of this Open WebUI instance (no trailing slash). "
                "Used to build the chat backlink appended to each message."
            ),
        )
        link_label: str = Field(
            default="Open conversation in Open WebUI →",
            description="Label for the clickable backlink appended to each message.",
        )
        request_timeout: int = Field(
            default=10,
            description="HTTP timeout for outbound requests, in seconds.",
        )

    def __init__(self) -> None:
        # Required when not using custom citations
        self.citation = False
        self.valves = self.Valves()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _build_backlink(self, owui_chat_id: str) -> str:
        base = self.valves.openwebui_url.rstrip("/") or "http://localhost:3000"
        if not owui_chat_id:
            return base
        return f"{base}/c/{owui_chat_id}"

    def _owui_chat_id(self, __metadata__: Optional[dict]) -> str:
        """Pull the current chat id from the metadata Open WebUI injects."""
        return (__metadata__ or {}).get("chat_id", "") or ""

    # ------------------------------------------------------------------
    # Outbound paths
    # ------------------------------------------------------------------
    def _send_via_bridge(
        self,
        text: str,
        owui_chat_id: str,
        include_backlink: bool,
    ) -> tuple[bool, str]:
        """Send through the telegram-bridge-openwebui sidecar."""
        url = self.valves.bridge_url.rstrip("/") + "/api/outbound"
        payload: dict[str, Any] = {
            "chat_id": owui_chat_id,
            "message": text,
            "include_backlink": include_backlink,
            "openwebui_url": self.valves.openwebui_url,
        }
        headers = {"Content-Type": "application/json"}
        if self.valves.bridge_token:
            headers["Authorization"] = f"Bearer {self.valves.bridge_token}"

        try:
            resp = requests.post(
                url, json=payload, headers=headers, timeout=self.valves.request_timeout
            )
        except requests.RequestException as e:
            return False, f"bridge request failed: {e}"

        if resp.status_code == 200:
            data = resp.json()
            tg_id = data.get("telegram_message_id", "?")
            return True, f"sent via bridge (telegram_message_id={tg_id})"
        try:
            err = resp.json().get("error", resp.text)
        except Exception:
            err = resp.text
        return False, f"bridge returned {resp.status_code}: {err}"

    def _send_direct(
        self,
        text: str,
        owui_chat_id: str,
        include_backlink: bool,
    ) -> tuple[bool, str]:
        """Fallback: call Telegram Bot API directly. No continuity."""
        if not self.valves.bot_token or not self.valves.chat_id:
            return False, "direct mode needs bot_token and chat_id in Valves"

        url = f"https://api.telegram.org/bot{self.valves.bot_token}/sendMessage"
        payload = {
            "chat_id": self.valves.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": "true",
        }
        try:
            resp = requests.post(url, data=payload, timeout=self.valves.request_timeout)
        except requests.RequestException as e:
            return False, f"telegram request failed: {e}"

        if resp.status_code == 200:
            return True, "sent directly (no continuity — replies start a new chat)"
        try:
            err = resp.json().get("description", resp.text)
        except Exception:
            err = resp.text
        return False, f"telegram API error {resp.status_code}: {err}"

    # ------------------------------------------------------------------
    # Tool method
    # ------------------------------------------------------------------
    async def send_telegram(
        self,
        message_content: str,
        title: Optional[str] = None,
        include_backlink: bool = True,
        __metadata__: Optional[dict] = None,
        __event_emitter__: Any = None,
    ) -> str:
        """
        Send a notification to Telegram. A clickable link back to the originating
        Open WebUI chat is appended automatically (disable per-call with
        include_backlink=False).

        When a bridge URL is configured, the message is sent via the
        telegram-bridge-openwebui sidecar so that Telegram replies continue
        the originating chat. Without the bridge, this falls back to direct
        Telegram Bot API calls (replies will start a new chat).

        :param message_content: The body of the Telegram message (Markdown supported).
        :param title: Optional short heading, prepended to the message.
        :param include_backlink: Set to False to skip the backlink (e.g. for sensitive content).
        :return: A short status string describing what happened.
        """
        owui_chat_id = self._owui_chat_id(__metadata__)

        # --- Validate config ------------------------------------------------
        use_bridge = bool(self.valves.bridge_url.strip())
        if not use_bridge and (not self.valves.bot_token or not self.valves.chat_id):
            msg = (
                "Telegram notifier is not configured. Either set 'bridge_url' "
                "(recommended) or set both 'bot_token' and 'chat_id' for direct mode."
            )
            if __event_emitter__:
                await __event_emitter__({
                    "type": "notification",
                    "data": {"type": "error", "content": msg},
                })
            return f"❌ {msg}"

        # --- Compose message ------------------------------------------------
        parts: list[str] = []
        if title:
            parts.append(f"*{title}*\n")
        parts.append(message_content)
        if include_backlink and owui_chat_id:
            parts.append(f"\n🔗 {self.valves.link_label}: {self._build_backlink(owui_chat_id)}")
        text = "\n".join(parts).strip()

        # --- Status: sending ------------------------------------------------
        if __event_emitter__:
            mode = "bridge" if use_bridge else "direct"
            await __event_emitter__({
                "type": "status",
                "data": {"description": f"Sending notification to Telegram ({mode})…", "done": False},
            })

        # --- Send -----------------------------------------------------------
        if use_bridge:
            ok, detail = self._send_via_bridge(text, owui_chat_id, include_backlink)
        else:
            ok, detail = self._send_direct(text, owui_chat_id, include_backlink)

        if ok:
            if __event_emitter__:
                await __event_emitter__({
                    "type": "status",
                    "data": {"description": "Telegram message sent ✅", "done": True},
                })
            backlink = (
                f" Backlink: {self._build_backlink(owui_chat_id)}"
                if include_backlink and owui_chat_id
                else ""
            )
            return f"✅ Sent to Telegram ({detail}).{backlink}"

        log.error("Telegram send failed: %s", detail)
        if __event_emitter__:
            await __event_emitter__({
                "type": "status",
                "data": {"description": f"Telegram send failed: {detail}", "done": True},
            })
        return f"❌ {detail}"