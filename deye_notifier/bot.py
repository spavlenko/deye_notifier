"""Telegram bot with inline buttons: subscribe/unsubscribe and statistics.

Works in private chats and groups. Inline buttons arrive as callback queries,
so they work in groups even with the bot's privacy mode on. In groups the
group itself is the subscriber, and only group admins may toggle it.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from .notify import TelegramBot, TelegramError
from .stats import build_stats, current_status
from .storage import Storage

log = logging.getLogger(__name__)

CB_STATS, CB_SUB, CB_UNSUB, CB_MENU = "stats", "sub", "unsub", "menu"

WELCOME = (
    "💡 Привіт! Я повідомляю, коли зникає та з'являється світло, "
    "про стан батареї та помилки інвертора."
)
COMMANDS = [
    ("menu", "Меню"),
    ("stats", "Статистика: відключення та якість мережі"),
    ("subscribe", "Підписатися на сповіщення"),
    ("unsubscribe", "Відписатися від сповіщень"),
]


def is_group(chat: dict[str, Any]) -> bool:
    return chat.get("type") in ("group", "supergroup")


def _chat_name(chat: dict[str, Any]) -> str:
    return (
        chat.get("title")
        or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
        or chat.get("username")
        or str(chat.get("id"))
    )


def keyboard(subscribed: bool, stats_view: bool = False) -> dict[str, Any]:
    toggle = (
        {"text": "🔕 Відписатися", "callback_data": CB_UNSUB}
        if subscribed
        else {"text": "🔔 Підписатися", "callback_data": CB_SUB}
    )
    first = (
        [{"text": "🔄 Оновити", "callback_data": CB_STATS},
         {"text": "⬅️ Меню", "callback_data": CB_MENU}]
        if stats_view
        else [{"text": "📊 Статистика", "callback_data": CB_STATS}]
    )
    return {"inline_keyboard": [first, [toggle]]}


class SubscriptionBot:
    def __init__(self, token: str, db_path: Path) -> None:
        self.api = TelegramBot(token)
        self.db_path = db_path
        self.storage: Storage | None = None  # opened in the bot's own thread

    # ---- views ------------------------------------------------------------

    def menu_text(self, chat: dict[str, Any]) -> str:
        assert self.storage is not None
        subscribed = self.storage.is_subscribed(chat["id"])
        who = "Група підписана" if is_group(chat) else "Ви підписані"
        status = f"✅ {who} на сповіщення." if subscribed else "🔕 Сповіщення вимкнені."
        lines = [WELCOME, "", status]
        if is_group(chat):
            lines.append("Підписку може змінити адміністратор групи.")
        return "\n".join(lines)

    def send_menu(self, chat: dict[str, Any]) -> None:
        assert self.storage is not None
        chat_id = chat["id"]
        if not is_group(chat) and self.storage.get(f"kb_removed:{chat_id}") != "1":
            # Remove the old reply keyboard from earlier versions of the bot.
            self.api.send_message(chat_id, "⌨️ Кнопки тепер під повідомленням 👇",
                                  reply_markup={"remove_keyboard": True})
            self.storage.set(f"kb_removed:{chat_id}", "1")
        self.api.send_message(chat_id, self.menu_text(chat),
                              reply_markup=keyboard(self.storage.is_subscribed(chat_id)))

    def send_stats(self, chat: dict[str, Any]) -> None:
        assert self.storage is not None
        self.api.send_message(chat["id"], build_stats(self.storage),
                              reply_markup=keyboard(self.storage.is_subscribed(chat["id"]), True))

    # ---- permissions & subscription ---------------------------------------

    def can_manage(self, chat: dict[str, Any], user: dict[str, Any] | None,
                   sender_chat: dict[str, Any] | None = None) -> bool:
        if not is_group(chat):
            return True
        if sender_chat and sender_chat.get("id") == chat["id"]:
            return True  # anonymous group admin
        if not user:
            return False
        try:
            return self.api.is_admin(chat["id"], user["id"])
        except TelegramError as e:
            log.error("getChatMember failed: %s", e)
            return False

    def set_subscription(self, chat: dict[str, Any], subscribe: bool) -> str:
        """Returns a short result text."""
        assert self.storage is not None
        if subscribe:
            if self.storage.add_subscriber(chat["id"], _chat_name(chat)):
                log.info("New subscriber: %s (%s)", _chat_name(chat), chat["id"])
                return "✅ Підписано"
            return "Вже підписано 👍"
        if self.storage.remove_subscriber(chat["id"]):
            log.info("Unsubscribed: %s (%s)", _chat_name(chat), chat["id"])
            return "🔕 Відписано"
        return "Підписки не було"

    # ---- update handlers --------------------------------------------------

    def handle_message(self, msg: dict[str, Any]) -> None:
        chat, text = msg.get("chat"), (msg.get("text") or "").strip()
        if chat and msg.get("migrate_to_chat_id"):  # group upgraded to supergroup
            assert self.storage is not None
            self.storage.migrate_subscriber(chat["id"], msg["migrate_to_chat_id"])
            log.info("Group %s migrated to %s", chat["id"], msg["migrate_to_chat_id"])
            return
        if not chat or not text.startswith("/"):
            return  # ignore normal chatter (only commands matter)
        cmd = text.split()[0].split("@")[0].lower()
        if cmd in ("/start", "/menu", "/help"):
            self.send_menu(chat)
        elif cmd in ("/stats", "/status"):
            self.send_stats(chat)
        elif cmd in ("/subscribe", "/unsubscribe"):
            if not self.can_manage(chat, msg.get("from"), msg.get("sender_chat")):
                self.api.send_message(chat["id"], "⛔ Лише адміністратори групи можуть змінювати підписку.")
                return
            result = self.set_subscription(chat, cmd == "/subscribe")
            self.api.send_message(chat["id"], result,
                                  reply_markup=keyboard(self.storage.is_subscribed(chat["id"])))

    def handle_callback(self, cq: dict[str, Any]) -> None:
        try:
            self._handle_callback(cq)
        except TelegramError as e:
            log.error("Callback %s failed: %s", cq.get("data"), e)
            try:  # stop the button's loading spinner
                self.api.answer_callback(cq["id"], "Помилка, спробуйте ще раз")
            except TelegramError:
                pass

    def _handle_callback(self, cq: dict[str, Any]) -> None:
        assert self.storage is not None
        msg = cq.get("message") or {}
        chat = msg.get("chat")
        data = cq.get("data")
        if not chat:
            self.api.answer_callback(cq["id"])
            return
        chat_id, message_id = chat["id"], msg["message_id"]

        if data == CB_STATS:
            self.api.edit_message(chat_id, message_id, build_stats(self.storage),
                                  keyboard(self.storage.is_subscribed(chat_id), stats_view=True))
            self.api.answer_callback(cq["id"], "Оновлено")
        elif data == CB_MENU:
            self.api.edit_message(chat_id, message_id, self.menu_text(chat),
                                  keyboard(self.storage.is_subscribed(chat_id)))
            self.api.answer_callback(cq["id"])
        elif data in (CB_SUB, CB_UNSUB):
            if not self.can_manage(chat, cq.get("from")):
                self.api.answer_callback(
                    cq["id"], "⛔ Лише адміністратори групи можуть змінювати підписку.", alert=True)
                return
            result = self.set_subscription(chat, data == CB_SUB)
            self.api.edit_message(chat_id, message_id, self.menu_text(chat),
                                  keyboard(self.storage.is_subscribed(chat_id)))
            self.api.answer_callback(cq["id"], result)
        else:
            self.api.answer_callback(cq["id"])

    def handle_member_update(self, upd: dict[str, Any]) -> None:
        """Bot added to a group -> post the menu there."""
        chat = upd.get("chat") or {}
        old = (upd.get("old_chat_member") or {}).get("status")
        new = (upd.get("new_chat_member") or {}).get("status")
        if is_group(chat) and old in ("left", "kicked", None) and new in ("member", "administrator"):
            log.info("Added to group %s (%s)", _chat_name(chat), chat.get("id"))
            self.send_menu(chat)
        elif new in ("left", "kicked") and self.storage and self.storage.remove_subscriber(chat.get("id")):
            log.info("Removed from %s; unsubscribed", _chat_name(chat))

    def handle_inline(self, query: dict[str, Any]) -> None:
        """@bot in any chat -> offer the current stats as a message to send."""
        assert self.storage is not None
        now = int(time.time())
        status = current_status(self.storage, now)
        stats = build_stats(self.storage, now)
        short = "\n".join(status)
        results = [
            {"type": "article", "id": f"stats-{now}", "title": "📊 Статистика",
             "description": short.replace("Зараз: ", ""),
             "input_message_content": {"message_text": stats}},
            {"type": "article", "id": f"now-{now}", "title": "💡 Зараз",
             "description": "Лише поточний стан",
             "input_message_content": {"message_text": short}},
        ]
        self.api.answer_inline(query["id"], results)

    def handle_update(self, update: dict[str, Any]) -> None:
        try:
            if "inline_query" in update:
                self.handle_inline(update["inline_query"])
            elif "callback_query" in update:
                self.handle_callback(update["callback_query"])
            elif "my_chat_member" in update:
                self.handle_member_update(update["my_chat_member"])
            elif "message" in update:
                self.handle_message(update["message"])
        except TelegramError as e:
            log.error("Telegram call failed while handling update: %s", e)

    # ---- loop -------------------------------------------------------------

    def run(self) -> None:
        self.storage = Storage(self.db_path)
        delay = 10
        while True:  # Telegram may be unreachable at boot / after sleep: keep retrying
            try:
                me = self.api.get_me()
                self.api.set_commands(COMMANDS)
                log.info("Telegram bot @%s is listening", me.get("username"))
                break
            except TelegramError as e:
                if e.status == 401:
                    log.error("Telegram token rejected (%s); bot disabled", e)
                    return
                log.error("Telegram bot can't start (%s); retrying in %ss", e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 300)

        offset_raw = self.storage.get("tg_offset")
        offset = int(offset_raw) if offset_raw else None
        while True:
            try:
                updates = self.api.get_updates(offset)
            except Exception as e:
                log.error("Telegram getUpdates failed: %s", e)
                time.sleep(10)
                continue
            for update in updates:
                try:
                    self.handle_update(update)
                except Exception:
                    log.exception("Failed to handle update %s", update.get("update_id"))
                offset = update["update_id"] + 1
                self.storage.set("tg_offset", str(offset))

    def start_in_background(self) -> threading.Thread:
        t = threading.Thread(target=self.run, name="telegram-bot", daemon=True)
        t.start()
        return t
