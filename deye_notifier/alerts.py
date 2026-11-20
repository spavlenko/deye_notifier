"""Watch the Deye Cloud alert list (inverter / battery / BMS faults) and notify on new ones."""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import DeyeClient
    from .notify import Notifier
    from .storage import Storage

log = logging.getLogger(__name__)

LOOKBACK = 24 * 3600  # how far back to ask for alerts each poll (API max is 30 days)
_BATTERY_RE = re.compile(r"bat|bms|soc|cell|lithium|li-?ion", re.I)


def clean_name(name: str | None) -> str:
    return re.sub(r"\s+", " ", name or "").strip() or "Unknown alert"


def is_battery_alert(alert: dict[str, Any]) -> bool:
    text = f"{alert.get('alertName', '')} {alert.get('description') or ''}"
    return bool(_BATTERY_RE.search(text))


def _ended(alert: dict[str, Any]) -> int | None:
    end = alert.get("alertEndTime")
    return int(end) if end else None


def _fmt(ts: int) -> str:
    from .monitor import fmt_ts
    return fmt_ts(ts)


def _duration(seconds: int) -> str:
    from .monitor import fmt_duration_uk
    return fmt_duration_uk(seconds)


def format_alert(alert: dict[str, Any], kind: str) -> str:
    """kind: 'start', 'end', or 'short' (started and ended between two polls)."""
    name = clean_name(alert.get("alertName"))
    code = alert.get("protocolName") or alert.get("alertCode") or ""
    title = f"{name} ({code})" if code else name
    battery = is_battery_alert(alert)
    start = int(alert.get("alertStartTime") or time.time())
    end = _ended(alert)

    if kind == "end":
        lines = [f"✅ {'Помилку батареї' if battery else 'Помилку інвертора'} усунено: {title}"]
        if end:
            lines.append(f"🕒 {_fmt(end)}")
            lines.append(f"⏱ Тривала: {_duration(end - start)}")
        return "\n".join(lines)

    head = "🔋⚠️ Помилка батареї" if battery else "⚠️ Помилка інвертора"
    lines = [f"{head}: {title}", f"🕒 {_fmt(start)}"]
    if kind == "short" and end:
        lines.append(f"✅ Вже усунено о {_fmt(end)} (тривала {_duration(end - start)})")
    for field, label in (("description", "ℹ️"), ("reason", "Причина:"), ("solution", "Що робити:")):
        if alert.get(field):
            lines.append(f"{label} {alert[field]}")
    return "\n".join(lines)


class AlertWatcher:
    def __init__(
        self,
        client: "DeyeClient",
        storage: "Storage",
        notifier: "Notifier",
        ignore: tuple[str, ...] = (),
    ) -> None:
        self.client = client
        self.storage = storage
        self.notifier = notifier
        self.ignore = tuple(i.lower() for i in ignore)

    def ignored(self, alert: dict[str, Any]) -> bool:
        name = clean_name(alert.get("alertName")).lower()
        return any(i in name for i in self.ignore)

    def check(self, device_sn: str) -> None:
        now = int(time.time())
        # First time: look back 30 days so statistics have alert history.
        backfilled = self.storage.get("alerts_backfilled") == "1"
        lookback = LOOKBACK if backfilled else 30 * 86400 - 60
        alerts = self.client.device_alerts(device_sn, now - lookback, now)
        if not backfilled:
            for a in alerts:
                if a.get("alertId"):
                    self.storage.save_alert_meta(str(a["alertId"]), clean_name(a.get("alertName")),
                                                 a.get("alertStartTime"))
                    if not self.storage.get_alert(str(a["alertId"]))["notified_start"]:
                        self.storage.save_alert(str(a["alertId"]), True, bool(_ended(a)))
            self.storage.set("alerts_backfilled", "1")
            self.storage.set("alerts_seeded", "1")
            return
        self.process(alerts)

    def process(self, alerts: list[dict[str, Any]]) -> None:
        first_run = self.storage.get("alerts_seeded") != "1"
        for alert in sorted(alerts, key=lambda a: a.get("alertStartTime") or 0):
            alert_id = str(alert.get("alertId") or "")
            if not alert_id:
                continue
            ended = _ended(alert)
            seen = self.storage.get_alert(alert_id)
            self.storage.save_alert_meta(alert_id, clean_name(alert.get("alertName")),
                                         alert.get("alertStartTime"))

            if first_run:
                # Don't spam history on the very first run; remember what already exists.
                self.storage.save_alert(alert_id, notified_start=True, notified_end=bool(ended))
                continue
            if self.ignored(alert):
                if not seen:
                    log.info("Ignoring alert %s", clean_name(alert.get("alertName")))
                    self.storage.save_alert(alert_id, notified_start=True, notified_end=True)
                continue

            if not seen:
                self.notifier.send(format_alert(alert, "short" if ended else "start"))
                self.storage.save_alert(alert_id, notified_start=True, notified_end=bool(ended))
            elif ended and not seen["notified_end"]:
                self.notifier.send(format_alert(alert, "end"))
                self.storage.save_alert(alert_id, notified_start=True, notified_end=True)

        if first_run:
            self.storage.set("alerts_seeded", "1")
            log.info("Alert history recorded (%d existing alerts, not notified)", len(alerts))
