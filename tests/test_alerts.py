import tempfile
import unittest
from pathlib import Path

from deye_notifier.alerts import AlertWatcher, format_alert, is_battery_alert
from deye_notifier.storage import Storage


class FakeNotifier:
    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)
        return 1


def alert(aid, name, start, end=None):
    return {"alertId": aid, "alertName": name, "protocolName": "F1",
            "alertStartTime": start, "alertEndTime": end}


class AlertTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "db.sqlite3")
        self.n = FakeNotifier()
        self.w = AlertWatcher(None, self.storage, self.n, ignore=("Grid_Phase_Wrong",))

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_run_seeds_without_notifying(self):
        self.w.process([alert("1", "W3\xa0Grid_Phase_Wrong", 100, 200),
                        alert("2", "F58 BMS_Comm_Fault", 100)])
        self.assertEqual(self.n.messages, [])
        # A fault still active at startup gets a "resolved" message when it ends.
        self.w.process([alert("2", "F58 BMS_Comm_Fault", 100, 900)])
        self.assertEqual(len(self.n.messages), 1)
        self.assertIn("усунено", self.n.messages[0])

    def test_new_alert_start_and_end(self):
        self.w.process([])  # seed
        self.w.process([alert("5", "F58 BMS_Comm_Fault", 1000)])
        self.w.process([alert("5", "F58 BMS_Comm_Fault", 1000)])  # no duplicate
        self.w.process([alert("5", "F58 BMS_Comm_Fault", 1000, 1600)])
        self.assertEqual(len(self.n.messages), 2)
        self.assertIn("🔋⚠️ Помилка батареї", self.n.messages[0])
        self.assertIn("усунено", self.n.messages[1])
        self.assertIn("10 хв", self.n.messages[1])

    def test_ignored_and_short(self):
        self.w.process([])
        self.w.process([alert("7", "W3\xa0Grid_Phase_Wrong", 1000, 1300),
                        alert("8", "F13 Grid_Mode_changed", 1000, 1300)])
        self.assertEqual(len(self.n.messages), 1)
        self.assertIn("⚠️ Помилка інвертора: F13 Grid_Mode_changed", self.n.messages[0])
        self.assertIn("Вже усунено", self.n.messages[0])

    def test_battery_detection(self):
        self.assertTrue(is_battery_alert({"alertName": "F56 DC_VoltLow_Fault Battery"}))
        self.assertFalse(is_battery_alert({"alertName": "W3 Grid_Phase_Wrong"}))
        self.assertIn("🔋", format_alert(alert("x", "BMS_Err", 1), "start"))


if __name__ == "__main__":
    unittest.main()
