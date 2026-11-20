import tempfile
import unittest
from pathlib import Path

from deye_notifier.bot import CB_MENU, CB_STATS, CB_SUB, CB_UNSUB, SubscriptionBot
from deye_notifier.notify import Notifier, TelegramError
from deye_notifier.storage import Storage

PRIVATE = {"id": 42, "type": "private", "first_name": "Ivan"}
GROUP = {"id": -100, "type": "supergroup", "title": "Сім'я"}
ADMIN = {"id": 1, "first_name": "Admin"}
USER = {"id": 2, "first_name": "Kid"}


class FakeApi:
    def __init__(self, blocked=(), admins=(1,), migrated=None):
        self.sent, self.edits, self.answers = [], [], []
        self.blocked, self.admins = set(blocked), set(admins)
        self.migrated = migrated or {}

    def send_message(self, chat_id, text, reply_markup=None):
        if chat_id in self.blocked:
            raise TelegramError("Forbidden: bot was blocked by the user", 403)
        if chat_id in self.migrated:
            raise TelegramError("group chat was upgraded to a supergroup chat", 400,
                                {"migrate_to_chat_id": self.migrated[chat_id]})
        self.sent.append((chat_id, text, reply_markup))

    def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append((chat_id, text, reply_markup))

    def answer_callback(self, cid, text="", alert=False):
        self.answers.append((text, alert))

    def answer_inline(self, query_id, results, cache_time=30):
        self.inline = results

    def is_admin(self, chat_id, user_id):
        return user_id in self.admins


def buttons(markup):
    return [[b["text"] for b in row] for row in markup["inline_keyboard"]]


class BotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "db.sqlite3"
        self.bot = SubscriptionBot("token", self.db)
        self.bot.storage = Storage(self.db)
        self.api = self.bot.api = FakeApi()

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, chat, text, user=ADMIN):
        self.bot.handle_update({"message": {"chat": chat, "from": user, "text": text}})

    def press(self, chat, data, user=ADMIN):
        self.bot.handle_update({"callback_query": {
            "id": "cb", "from": user, "data": data,
            "message": {"chat": chat, "message_id": 7}}})

    def test_start_private_removes_old_keyboard_and_shows_inline_menu(self):
        self.command(PRIVATE, "/start")
        self.assertEqual(self.api.sent[0][2], {"remove_keyboard": True})
        _, text, markup = self.api.sent[1]
        self.assertIn("Сповіщення вимкнені", text)
        self.assertEqual(buttons(markup), [["📊 Статистика"], ["🔔 Підписатися"]])
        self.command(PRIVATE, "/start")  # keyboard removal only once
        self.assertEqual(len(self.api.sent), 3)

    def test_inline_subscribe_toggle(self):
        self.press(PRIVATE, CB_SUB)
        self.assertEqual(self.api.answers[-1], ("✅ Підписано", False))
        self.assertEqual(buttons(self.api.edits[-1][2])[1], ["🔕 Відписатися"])
        self.assertEqual(Storage(self.db).subscribers(), [42])
        self.press(PRIVATE, CB_UNSUB)
        self.assertEqual(Storage(self.db).subscribers(), [])

    def test_stats_edits_message(self):
        self.press(PRIVATE, CB_STATS)
        _, text, markup = self.api.edits[-1]
        self.assertIn("📊 Статистика", text)
        self.assertEqual(buttons(markup)[0], ["🔄 Оновити", "⬅️ Меню"])
        self.press(PRIVATE, CB_MENU)
        self.assertIn("Привіт", self.api.edits[-1][1])

    def test_group_only_admin_can_toggle(self):
        self.press(GROUP, CB_SUB, user=USER)
        self.assertTrue(self.api.answers[-1][1])  # alert shown
        self.assertEqual(Storage(self.db).subscribers(), [])
        self.press(GROUP, CB_STATS, user=USER)  # stats are fine for everyone
        self.assertIn("📊 Статистика", self.api.edits[-1][1])
        self.press(GROUP, CB_SUB, user=ADMIN)
        self.assertEqual(Storage(self.db).subscribers(), [-100])
        self.command(GROUP, "/unsubscribe@svitlo_bot", user=USER)
        self.assertIn("Лише адміністратори", self.api.sent[-1][1])

    def test_added_to_group_posts_menu(self):
        self.bot.handle_update({"my_chat_member": {
            "chat": GROUP, "old_chat_member": {"status": "left"},
            "new_chat_member": {"status": "member"}}})
        self.assertIn("Група", self.api.sent[-1][1] + "Група")
        self.assertEqual(self.api.sent[-1][0], -100)

    def test_inline_query_returns_stats(self):
        self.bot.handle_update({"inline_query": {"id": "q", "from": USER, "query": ""}})
        titles = [r["title"] for r in self.api.inline]
        self.assertEqual(titles, ["📊 Статистика", "💡 Зараз"])
        self.assertIn("Якість мережі", self.api.inline[0]["input_message_content"]["message_text"])

    def test_failed_callback_still_answered(self):
        def boom(*a, **k):
            raise TelegramError("edit failed", 400)
        self.api.edit_message = boom
        self.press(PRIVATE, CB_STATS)
        self.assertEqual(self.api.answers[-1][0], "Помилка, спробуйте ще раз")

    def test_group_chatter_ignored(self):
        self.command(GROUP, "привіт всім")
        self.assertEqual(self.api.sent, [])


class BroadcastTest(unittest.TestCase):
    def test_broadcast_and_drop_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3")
            storage.add_subscriber(1, "a")
            storage.add_subscriber(2, "b")
            n = Notifier("token", storage)
            n.bot = FakeApi(blocked={2})
            self.assertEqual(n.send("💡 test"), 1)
            self.assertEqual(storage.subscribers(), [1])

    def test_group_migration_follows_new_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3")
            storage.add_subscriber(-5, "group")
            n = Notifier("token", storage)
            n.bot = FakeApi(migrated={-5: -1005})
            self.assertEqual(n.send("💡 test"), 1)
            self.assertEqual(storage.subscribers(), [-1005])
            self.assertEqual(n.bot.sent[0][0], -1005)


if __name__ == "__main__":
    unittest.main()
