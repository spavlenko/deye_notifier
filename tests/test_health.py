import tempfile
import unittest
from pathlib import Path

from deye_notifier.detector import GridState, Reading
from deye_notifier.monitor import Monitor
from deye_notifier.stats import build_stats, plural_uk
from deye_notifier.storage import Storage
from tests.test_monitor import FakeNotifier, make_cfg

T = 1_700_000_000


def reading(ts, state=GridState.ON, soc=50, **metrics):
    m = {"grid_v1": 230, "grid_v2": 231, "grid_v3": 229, "battery_temp": 25, "inverter_temp": 40}
    m.update(metrics)
    return Reading(state, ts, "", soc, 12000, {"L1": 100, "L2": 100, "L3": 100}, m)


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "db.sqlite3")
        self.n = FakeNotifier()
        self.m = Monitor(make_cfg(self.tmp.name), None, self.storage, self.n)

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self, readings):
        for r in readings:
            self.m.handle(r, now=r.collected_at)

    def test_not_charging(self):
        # Grid on, SOC stuck at 40% for 40 minutes -> one alert.
        self.feed([reading(T + i * 300, soc=40) for i in range(9)])
        msgs = [x for x in self.n.messages if "не заряджається" in x]
        self.assertEqual(len(msgs), 1)
        self.assertIn("40% → 40%", msgs[0])

    def test_charging_ok(self):
        self.feed([reading(T + i * 300, soc=40 + i) for i in range(9)])
        self.assertFalse(any("не заряджається" in x for x in self.n.messages))

    def test_not_charging_needs_grid_on_whole_window(self):
        rs = [reading(T + i * 300, soc=40) for i in range(9)]
        rs[3] = reading(T + 900, GridState.OFF, soc=40)
        self.feed(rs)
        self.assertFalse(any("не заряджається" in x for x in self.n.messages))

    def test_temperature_high_and_recover(self):
        self.feed([reading(T, battery_temp=30), reading(T + 300, battery_temp=46),
                   reading(T + 600, battery_temp=47), reading(T + 900, battery_temp=41),
                   reading(T + 1200, inverter_temp=72)])
        temp = [x for x in self.n.messages if "Температура" in x or "температура" in x]
        self.assertEqual(len(temp), 3)
        self.assertIn("Висока температура батареї: 46°C", temp[0])
        self.assertIn("в нормі: 41°C", temp[1])
        self.assertIn("Висока температура інвертора: 72°C", temp[2])

    def test_battery_cold(self):
        self.feed([reading(T, battery_temp=3)])
        self.assertIn("Низька температура батареї", self.n.messages[0])

    def test_stats_message(self):
        self.feed([reading(T, soc=90), reading(T + 300, GridState.OFF, soc=85),
                   reading(T + 3900, GridState.ON, soc=60, grid_v1=260)])
        self.storage.save_alert_meta("a1", "W3 Grid_Phase_Wrong", T + 100)
        text = build_stats(self.storage, now=T + 4000)
        self.assertIn("📊 Статистика", text)
        self.assertIn("1 раз, 1 год 0 хв", text)
        self.assertIn("L1: 230–260 В", text)
        self.assertIn("Поза нормою (207–253 В): 50% часу", text)
        self.assertIn("Збої фаз (Grid_Phase_Wrong): 1 раз", text)
        self.assertIn("🔋 60%", text)

    def test_plural(self):
        self.assertEqual([plural_uk(n, "раз", "рази", "разів") for n in (1, 2, 5, 11, 21, 22)],
                         ["раз", "рази", "разів", "разів", "раз", "рази"])


if __name__ == "__main__":
    unittest.main()
