"""Notification backends: always log; optionally broadcast via the Telegram Bot API."""

from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .storage import Storage

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, params: dict | None = None):
        super().__init__(message)
        self.status = status
        params = params or {}
        self.migrate_to: int | None = params.get("migrate_to_chat_id")  # group -> supergroup
        self.retry_after: int | None = params.get("retry_after")  # 429 rate limit


class TelegramBot:
    """Minimal Telegram Bot API client (https://core.telegram.org/bots/api)."""

    def __init__(self, token: str, timeout: int = 15) -> None:
        self.token = token
        self.timeout = timeout

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: int | None = None) -> Any:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(params or {}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                payload = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode())
            except (ValueError, OSError):
                body = {}
            desc = body.get("description") or ""
            raise TelegramError(f"{method}: HTTP {e.code} {desc}".strip(), e.code,
                                body.get("parameters")) from e
        except (OSError, http.client.HTTPException, ValueError) as e:  # URLError, timeouts, resets
            raise TelegramError(f"{method}: {e}") from e
        if not payload.get("ok"):
            raise TelegramError(f"{method}: {payload.get('description')}", payload.get("error_code"),
                                payload.get("parameters"))
        return payload["result"]

    def get_me(self) -> dict[str, Any]:
        return self.call("getMe")

    def get_updates(self, offset: int | None, poll_timeout: int = 50) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": poll_timeout, "allowed_updates": ["message", "callback_query", "my_chat_member", "inline_query"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", params, timeout=poll_timeout + 10)

    def send_message(self, chat_id: int | str, text: str, reply_markup: dict | None = None) -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup:
            params["reply_markup"] = reply_markup
        self.call("sendMessage", params)

    def edit_message(self, chat_id: int, message_id: int, text: str,
                     reply_markup: dict | None = None) -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup:
            params["reply_markup"] = reply_markup
        try:
            self.call("editMessageText", params)
        except TelegramError as e:
            if "message is not modified" not in str(e):
                raise

    def answer_callback(self, callback_id: str, text: str = "", alert: bool = False) -> None:
        self.call("answerCallbackQuery",
                  {"callback_query_id": callback_id, "text": text, "show_alert": alert})

    def answer_inline(self, query_id: str, results: list[dict[str, Any]], cache_time: int = 30) -> None:
        self.call("answerInlineQuery", {"inline_query_id": query_id, "results": results,
                                        "cache_time": cache_time, "is_personal": False})

    def is_admin(self, chat_id: int, user_id: int) -> bool:
        member = self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})
        return member.get("status") in ("creator", "administrator")

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        self.call("setMyCommands",
                  {"commands": [{"command": c, "description": d} for c, d in commands]})


class Notifier:
    """Logs every message and sends it to all Telegram subscribers."""

    def __init__(self, telegram_token: str | None, storage: "Storage"):
        self.bot = TelegramBot(telegram_token) if telegram_token else None
        self.storage = storage

    def send(self, text: str) -> int:
        """Returns how many subscribers received the message."""
        log.warning("NOTIFY: %s", text.replace("\n", " | "))
        if not self.bot:
            return 0
        delivered = 0
        for chat_id in self.storage.subscribers():
            if self._send_one(chat_id, text):
                delivered += 1
        return delivered

    def _send_one(self, chat_id: int, text: str, attempt: int = 0) -> bool:
        try:
            self.bot.send_message(chat_id, text)
            return True
        except TelegramError as e:  # never let notifications kill the monitor
            if e.migrate_to and attempt == 0:
                # Group was upgraded to a supergroup: its chat id changed.
                log.info("Chat %s migrated to %s", chat_id, e.migrate_to)
                self.storage.migrate_subscriber(chat_id, e.migrate_to)
                return self._send_one(e.migrate_to, text, attempt + 1)
            if e.retry_after and attempt < 2:
                time.sleep(min(e.retry_after, 30))
                return self._send_one(chat_id, text, attempt + 1)
            if e.status == 403:  # user blocked the bot / bot removed from group
                log.info("Removing subscriber %s: %s", chat_id, e)
                self.storage.remove_subscriber(chat_id)
            else:
                log.error("Telegram send to %s failed: %s", chat_id, e)
            return False
