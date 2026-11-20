"""Polling loop: read inverter data, detect grid transitions, record and notify."""

from __future__ import annotations

import logging
import time
from datetime import datetime

from .alerts import AlertWatcher
from .client import DeyeClient, DeyeError
from .config import Config
from .detector import GridState, Reading, detect
from .notify import Notifier
from .storage import Storage

log = logging.getLogger(__name__)


def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def fmt_duration_uk(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    return f"{h} год {m} хв" if h else f"{m} хв"


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def resolve_device_sn(client: DeyeClient, cfg: Config) -> str:
    if cfg.device_sn:
        return cfg.device_sn
    for station in client.station_with_devices():
        if cfg.station_id and str(station.get("id")) != str(cfg.station_id):
            continue
        for dev in station.get("deviceListItems") or station.get("deviceList") or []:
            if str(dev.get("deviceType", "")).upper() == "INVERTER" and dev.get("deviceSn"):
                log.info("Using inverter %s from station %s", dev["deviceSn"], station.get("name"))
                return dev["deviceSn"]
    raise SystemExit(
        "Could not auto-detect an inverter. Set DEYE_DEVICE_SN (the inverter serial "
        "number, visible in the Deye Cloud app) in .env"
    )


class Monitor:
    def __init__(self, cfg: Config, client: DeyeClient, storage: Storage, notifier: Notifier):
        self.cfg = cfg
        self.client = client
        self.storage = storage
        self.notifier = notifier
        self.device_sn: str | None = None
        self.alerts = (
            AlertWatcher(client, storage, notifier, cfg.alert_ignore) if cfg.alerts_enabled else None
        )

    def poll_once(self) -> Reading:
        if not self.device_sn:
            self.device_sn = resolve_device_sn(self.client, self.cfg)
        device = self.client.device_latest(self.device_sn)
        reading = detect(device, self.cfg.grid_voltage_threshold, self.cfg.grid_voltage_keys)
        self.handle(reading, now=int(time.time()))
        if self.alerts:
            try:
                self.alerts.check(self.device_sn)
            except Exception as e:  # alerts must never break grid monitoring
                log.error("Alert check failed: %s", e)
        return reading

    def handle(self, reading: Reading, now: int) -> None:
        # Stale data: the inverter/dongle stopped reporting. Don't guess the grid state.
        if reading.collected_at and now - reading.collected_at > self.cfg.stale_after:
            if self.storage.get("stale_notified") != "1":
                self.notifier.send(
                    f"⚠️ Дані Deye не оновлюються з {fmt_ts(reading.collected_at)}.\n"
                    "Стан мережі невідомий — інвертор або інтернет офлайн "
                    "(можливо, зникло світло і роутер без живлення)."
                )
                self.storage.set("stale_notified", "1")
            return
        if self.storage.get("stale_notified") == "1":
            self.storage.set("stale_notified", "0")
            log.info("Data is fresh again")
            grid = {GridState.ON: "💡 світло є", GridState.OFF: "💡❌ світла немає"}.get(
                reading.state, "стан мережі невідомий")
            self.notifier.send(f"📶 Дані Deye знову надходять: {grid}\n🕒 {fmt_ts(reading.collected_at or now)}")

        ts = reading.collected_at or now
        last_ts = self.storage.get("last_collected_at")
        if last_ts and reading.collected_at and int(last_ts) == reading.collected_at:
            log.debug("No new data since %s", fmt_ts(reading.collected_at))
            return
        self.storage.set("last_collected_at", str(ts))

        if reading.metrics is not None:
            self.storage.add_reading(ts, {**reading.metrics, "soc": reading.soc,
                                          "grid_state": reading.state.value})
        self.check_battery(reading, ts)
        self.check_overload(reading, ts)
        self.check_temperatures(reading, ts)
        self.check_charging(reading, ts)

        if reading.state is GridState.UNKNOWN:
            log.warning("Grid state unknown: %s", reading.detail)
            return

        previous = self.storage.get("grid_state")
        self.storage.set("grid_state", reading.state.value)
        log.info("Grid %s (%s), battery %s%%", reading.state.value.upper(), reading.detail,
                 "?" if reading.soc is None else f"{reading.soc:g}")

        open_outage = self.storage.open_outage()
        if reading.state is GridState.OFF and not open_outage:
            self.storage.start_outage(ts)
            self.notifier.send(f"💡❌ Світло зникло\n🕒 {fmt_ts(ts)}")
        elif reading.state is GridState.ON and open_outage:
            done = self.storage.end_outage(open_outage, ts)
            self.notifier.send(
                f"💡✅ Світло з'явилося\n🕒 {fmt_ts(ts)}\n"
                f"⏱ Без світла: {fmt_duration_uk(done.duration)} (з {fmt_ts(done.started_at)})"
            )
        elif previous is None:
            log.info("Initial state recorded: grid %s", reading.state.value)

    BATTERY_REARM = 5  # % above the threshold before a new low-battery alert is allowed

    def check_battery(self, reading: Reading, ts: int) -> None:
        limit = self.cfg.battery_low_soc
        if not limit or reading.soc is None:
            return
        alerted = self.storage.get("battery_low_notified") == "1"
        if reading.soc < limit and not alerted:
            text = f"🪫 Низький заряд батареї: {reading.soc:g}%\n🕒 {fmt_ts(ts)}"
            if reading.state is GridState.OFF:
                text += "\n💡❌ Світла немає — економте енергію."
            self.notifier.send(text)
            self.storage.set("battery_low_notified", "1")
        elif alerted and reading.soc >= limit + self.BATTERY_REARM:
            log.info("Battery recovered to %g%%, low-battery alert re-armed", reading.soc)
            self.storage.set("battery_low_notified", "0")

    OVERLOAD_REARM = 10  # % below the threshold before a new overload alert is allowed

    def check_overload(self, reading: Reading, ts: int) -> None:
        """Warn when the inverter carries close to its capacity while on battery."""
        limit = self.cfg.overload_percent
        if not limit or not reading.rated_power or not reading.load_phases:
            return
        alerted = self.storage.get("overload_notified") == "1"
        if reading.state is not GridState.OFF:
            # On grid, heavy loads pass through from the grid; only re-arm here.
            if alerted:
                self.storage.set("overload_notified", "0")
            return

        rated = reading.rated_power
        per_phase = rated / len(reading.load_phases)
        total = sum(reading.load_phases.values())
        usage = {ph: w / per_phase * 100 for ph, w in reading.load_phases.items()}
        total_pct = total / rated * 100
        worst = max([*usage.values(), total_pct])

        if worst >= limit and not alerted:
            kw = lambda w: f"{w / 1000:.1f}".rstrip("0").rstrip(".")  # noqa: E731
            lines = ["⚡️ Перевантаження інвертора!", f"🕒 {fmt_ts(ts)}"]
            for ph, w in reading.load_phases.items():
                mark = " ⚠️" if usage[ph] >= limit else ""
                lines.append(f"Фаза {ph}: {kw(w)} з {kw(per_phase)} кВт ({usage[ph]:.0f}%){mark}")
            lines.append(f"Загалом: {kw(total)} з {kw(rated)} кВт ({total_pct:.0f}%)")
            lines.append("💡❌ Працюємо від батареї — вимкніть потужні прилади.")
            self.notifier.send("\n".join(lines))
            self.storage.set("overload_notified", "1")
        elif alerted and worst < limit - self.OVERLOAD_REARM:
            log.info("Load back to normal (%.0f%%), overload alert re-armed", worst)
            self.storage.set("overload_notified", "0")

    TEMP_REARM = 3  # °C back inside the range before "back to normal" + re-arm

    def _band_alert(self, key: str, value: float | None, high: float | None, low: float | None,
                    label: str, ts: int) -> None:
        """Generic too-high / too-low alert with hysteresis and a 'back to normal' message."""
        if value is None:
            return
        state = self.storage.get(key) or "ok"
        if high and value >= high and state != "high":
            self.notifier.send(f"🌡️ Висока температура {label}: {value:g}°C (норма до {high:g}°C)\n"
                               f"🕒 {fmt_ts(ts)}")
            self.storage.set(key, "high")
        elif low is not None and value <= low and state != "low":
            self.notifier.send(f"🥶 Низька температура {label}: {value:g}°C (норма від {low:g}°C)\n"
                               f"🕒 {fmt_ts(ts)}\nЗаряд батареї може бути обмежено.")
            self.storage.set(key, "low")
        elif (state == "high" and high and value <= high - self.TEMP_REARM) or (
            state == "low" and low is not None and value >= low + self.TEMP_REARM
        ):
            self.notifier.send(f"✅ Температура {label} в нормі: {value:g}°C\n🕒 {fmt_ts(ts)}")
            self.storage.set(key, "ok")

    def check_temperatures(self, reading: Reading, ts: int) -> None:
        m = reading.metrics or {}
        self._band_alert("temp_battery", m.get("battery_temp"), self.cfg.battery_temp_max,
                         self.cfg.battery_temp_min, "батареї", ts)
        self._band_alert("temp_inverter", m.get("inverter_temp"), self.cfg.inverter_temp_max,
                         None, "інвертора", ts)

    def check_charging(self, reading: Reading, ts: int) -> None:
        """Grid has been on for N minutes, battery isn't full, and SOC isn't rising."""
        minutes = self.cfg.not_charging_minutes
        if not minutes or reading.soc is None:
            return
        alerted = self.storage.get("not_charging_notified") == "1"
        if reading.state is not GridState.ON:
            if alerted:
                self.storage.set("not_charging_notified", "0")
            return
        baseline = self.storage.reading_at_or_before(ts - minutes * 60)
        if alerted:
            # Re-arm once charging is visibly happening again.
            ref = self.storage.get("not_charging_soc")
            rising = ref is not None and reading.soc >= float(ref) + 2
            if rising or reading.soc >= self.cfg.not_charging_below_soc:
                log.info("Battery is charging again (%g%%)", reading.soc)
                self.storage.set("not_charging_notified", "0")
            return
        if not baseline or baseline["soc"] is None or reading.soc >= self.cfg.not_charging_below_soc:
            return
        window = self.storage.readings(baseline["ts"], ts)
        if any(r["grid_state"] != GridState.ON.value for r in window):
            return  # grid hasn't been on for the whole window
        if reading.soc <= baseline["soc"]:
            mins = (ts - baseline["ts"]) // 60
            self.notifier.send(
                "🔋❗ Батарея не заряджається\n"
                f"💡 Світло є вже {fmt_duration_uk(mins * 60)}, а заряд не зростає: "
                f"{baseline['soc']:g}% → {reading.soc:g}%\n"
                "Перевірте налаштування заряду від мережі або BMS."
            )
            self.storage.set("not_charging_notified", "1")
            self.storage.set("not_charging_soc", str(reading.soc))

    def run(self) -> None:
        log.info("Monitoring every %ss", self.cfg.poll_interval)
        backoff = self.cfg.poll_interval
        while True:
            try:
                self.poll_once()
                backoff = self.cfg.poll_interval
            except DeyeError as e:
                log.error("Deye API error: %s", e)
                backoff = min(backoff * 2, 1800)
            except Exception:  # keep monitoring no matter what; the log has the traceback
                log.exception("Unexpected error in poll")
                backoff = min(backoff * 2, 1800)
            time.sleep(backoff)
