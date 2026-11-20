import tempfile
import unittest
from pathlib import Path

from deye_notifier.config import Config
from deye_notifier.detector import GridState, detect
from deye_notifier.monitor import Monitor
from deye_notifier.storage import Storage


def device(ts, relay=None, soc=None, **volts):
    data = [{"key": k, "value": str(v), "unit": "V"} for k, v in volts.items()]
    data.append({"key": "LoadVoltageL1", "value": "230", "unit": "V"})
    if relay is not None:
        data.append({"key": "GridRelayStatus", "value": relay, "unit": ""})
    if soc is not None:
        data.append({"key": "SOC", "value": str(soc), "unit": "%"})
    return {"deviceSn": "X", "collectionTime": ts, "dataList": data}


class FakeNotifier:
    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)


def make_cfg(tmp, **over):
    base = dict(app_id="a", app_secret="b", email="c", password="d", base_url="u",
                station_id=None, device_sn="X", poll_interval=60, grid_voltage_threshold=100.0,
                grid_voltage_keys=(), stale_after=900, battery_low_soc=45.0,
                overload_percent=80.0, alerts_enabled=False, alert_ignore=(),
                data_dir=Path(tmp), telegram_token=None)
    base.update(over)
    return Config(**base)


class DetectTest(unittest.TestCase):
    def test_on_off(self):
        self.assertEqual(detect(device(1, GridVoltageL1=231), 100).state, GridState.ON)
        self.assertEqual(detect(device(1, GridVoltageL1=0), 100).state, GridState.OFF)

    def test_load_voltage_ignored(self):
        d = device(1)
        self.assertEqual(detect(d, 100).state, GridState.UNKNOWN)

    def test_prefers_grid_keys_over_ac_output(self):
        # Outage: grid side 0V, inverter AC output still 230V from battery.
        d = device(1, GridVoltageL1=0, ACVoltageRUA=230)
        self.assertEqual(detect(d, 100).state, GridState.OFF)

    def test_relay_status(self):
        on = device(1, relay="Pull-in", GridVoltageL1=230)
        self.assertEqual(detect(on, 100).state, GridState.ON)
        # Relay open while grid side still shows voltage -> house is off-grid.
        self.assertEqual(detect(device(1, relay="Pull-out", GridVoltageL1=230), 100).state,
                         GridState.OFF)
        # Relay only, no voltage fields.
        self.assertEqual(detect(device(1, relay="Pull-in"), 100).state, GridState.ON)
        self.assertEqual(detect(device(1, relay="Break"), 100).state, GridState.OFF)

    def test_no_voltage_overrides_relay(self):
        self.assertEqual(detect(device(1, relay="Pull-in", GridVoltageL1=0), 100).state,
                         GridState.OFF)

    def test_explicit_keys(self):
        d = device(1, Foo=0)
        self.assertEqual(detect(d, 100, ("Foo",)).state, GridState.OFF)

    def test_ms_timestamp(self):
        self.assertEqual(detect(device(1_700_000_000_000, GridVoltageL1=230), 100).collected_at,
                         1_700_000_000)


class MonitorTest(unittest.TestCase):
    def test_outage_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            storage = Storage(Path(tmp) / "db.sqlite3")
            n = FakeNotifier()
            m = Monitor(cfg, None, storage, n)
            t = 1_700_000_000
            m.handle(detect(device(t, GridVoltageL1=230), 100), now=t)
            m.handle(detect(device(t + 300, GridVoltageL1=0), 100), now=t + 300)
            m.handle(detect(device(t + 300, GridVoltageL1=0), 100), now=t + 360)  # duplicate
            m.handle(detect(device(t + 3900, GridVoltageL1=229), 100), now=t + 3900)
            self.assertEqual(len(n.messages), 2)
            self.assertIn("Світло зникло", n.messages[0])
            self.assertIn("💡", n.messages[0])
            self.assertIn("Світло з'явилося", n.messages[1])
            self.assertIn("1 год 0 хв", n.messages[1])
            (o,) = storage.outages()
            self.assertEqual((o.started_at, o.ended_at), (t + 300, t + 3900))

    def test_stale_data_does_not_flip(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3")
            n = FakeNotifier()
            m = Monitor(make_cfg(tmp), None, storage, n)
            m.handle(detect(device(1000, GridVoltageL1=0), 100), now=5000)
            m.handle(detect(device(1000, GridVoltageL1=0), 100), now=6000)
            self.assertEqual(len(n.messages), 1)
            self.assertIn("не оновлюються", n.messages[0])
            self.assertEqual(storage.outages(), [])
            m.handle(detect(device(6100, GridVoltageL1=230), 100), now=6150)
            self.assertIn("знову надходять: 💡 світло є", n.messages[-1])


class BatteryTest(unittest.TestCase):
    def test_low_battery_alert_once_and_rearm(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3")
            n = FakeNotifier()
            m = Monitor(make_cfg(tmp), None, storage, n)
            t = 1_700_000_000
            for i, (soc, volts) in enumerate([(60, 230), (44, 0), (40, 0), (48, 230),
                                              (44, 230), (50, 230), (43, 230)]):
                m.handle(detect(device(t + i * 300, soc=soc, GridVoltageL1=volts), 100),
                         now=t + i * 300)
            low = [x for x in n.messages if "Низький заряд" in x]
            self.assertEqual(len(low), 2)  # 44% (first time) and 43% (after re-arm at 50%)
            self.assertIn("44%", low[0])
            self.assertIn("Світла немає", low[0])
            self.assertNotIn("Світла немає", low[1])


class OverloadTest(unittest.TestCase):
    def run_loads(self, steps):
        from deye_notifier.detector import Reading
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3")
            n = FakeNotifier()
            m = Monitor(make_cfg(tmp), None, storage, n)
            for state, loads in steps:
                m.check_overload(Reading(state, 1, "", 80, 12000, loads), 1)
            return n.messages

    def test_phase_overload_only_off_grid(self):
        off, on = GridState.OFF, GridState.ON
        msgs = self.run_loads([
            (on, {"L1": 3900, "L2": 0, "L3": 0}),      # on grid: ignored
            (off, {"L1": 3300, "L2": 100, "L3": 100}),  # 82% of 4 kW -> alert
            (off, {"L1": 3500, "L2": 100, "L3": 100}),  # still high: no repeat
            (off, {"L1": 2500, "L2": 100, "L3": 100}),  # 62% -> re-arm
            (off, {"L1": 3400, "L2": 100, "L3": 100}),  # alert again
        ])
        self.assertEqual(len(msgs), 2)
        self.assertIn("Перевантаження", msgs[0])
        self.assertIn("Фаза L1: 3.3 з 4 кВт (82%) ⚠️", msgs[0])

    def test_total_overload(self):
        msgs = self.run_loads([(GridState.OFF, {"L1": 3200, "L2": 3200, "L3": 3300})])
        self.assertEqual(len(msgs), 1)
        self.assertIn("Загалом: 9.7 з 12 кВт (81%)", msgs[0])


if __name__ == "__main__":
    unittest.main()
