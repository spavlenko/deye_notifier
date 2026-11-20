"""CLI entry point: python -m deye_notifier {run,once,dump,stations,report}."""

from __future__ import annotations

import argparse
import json
import logging
import time

from .client import DeyeClient
from .config import Config
from .detector import find_grid_voltage_keys, flatten_data_list
from .monitor import Monitor, fmt_duration, fmt_ts, resolve_device_sn
from .bot import SubscriptionBot
from .notify import Notifier, TelegramBot, TelegramError
from .storage import Storage


def build(cfg: Config) -> tuple[DeyeClient, Storage]:
    client = DeyeClient(
        cfg.base_url, cfg.app_id, cfg.app_secret, cfg.email, cfg.password,
        token_file=cfg.data_dir / "token.json",
    )
    return client, Storage(db_path(cfg))


def db_path(cfg: Config):
    return cfg.data_dir / "deye.sqlite3"


def cmd_stations(cfg: Config) -> None:
    client, _ = build(cfg)
    for st in client.station_with_devices():
        print(f"Station {st.get('id')}: {st.get('name')}")
        for dev in st.get("deviceListItems") or st.get("deviceList") or []:
            print(f"  {dev.get('deviceType')}: {dev.get('deviceSn')}")


def cmd_dump(cfg: Config) -> None:
    client, _ = build(cfg)
    sn = resolve_device_sn(client, cfg)
    device = client.device_latest(sn)
    if not device:
        raise SystemExit(f"No data for device {sn}")
    data = flatten_data_list(device)
    print(f"Device {sn}, collected {device.get('collectionTime')}")
    for key, (value, unit) in sorted(data.items()):
        print(f"  {key:40} {value} {unit}")
    print("\nAuto-detected grid voltage keys:", find_grid_voltage_keys(data) or "none")


def cmd_once(cfg: Config) -> None:
    client, storage = build(cfg)
    reading = Monitor(cfg, client, storage, Notifier(cfg.telegram_token, storage)).poll_once()
    print(json.dumps({"state": reading.state.value, "collected_at": reading.collected_at,
                      "detail": reading.detail, "battery_soc": reading.soc}, indent=2))


def cmd_run(cfg: Config) -> None:
    client, storage = build(cfg)
    if cfg.telegram_token:
        SubscriptionBot(cfg.telegram_token, db_path(cfg)).start_in_background()
    else:
        logging.info("TELEGRAM_BOT_TOKEN not set; notifications go to the log only")
    Monitor(cfg, client, storage, Notifier(cfg.telegram_token, storage)).run()


def cmd_bot(cfg: Config) -> None:
    if not cfg.telegram_token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env first")
    SubscriptionBot(cfg.telegram_token, db_path(cfg)).run()


def cmd_report(cfg: Config, days: int) -> None:
    storage = Storage(db_path(cfg))
    since = int(time.time()) - days * 86400
    outages = storage.outages(since)
    if not outages:
        print(f"No outages in the last {days} days.")
        return
    total = 0
    for o in outages:
        end = fmt_ts(o.ended_at) if o.ended_at else "ongoing"
        print(f"{fmt_ts(o.started_at)}  →  {end:16}  {fmt_duration(o.duration)}")
        total += o.duration
    print(f"\n{len(outages)} outage(s), {fmt_duration(total)} without grid in the last {days} days.")


def cmd_telegram_test(cfg: Config) -> None:
    if not cfg.telegram_token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env first")
    storage = Storage(db_path(cfg))
    subs = storage.subscribers()
    if not subs:
        raise SystemExit("No subscribers yet. Run `python3 -m deye_notifier bot`, open your bot "
                         "in Telegram and press /start → 🔔 Підписатися.")
    n = Notifier(cfg.telegram_token, storage).send(
        "💡 Тестове повідомлення від deye_notifier — сповіщення працюють!")
    print(f"Sent to {n} of {len(subs)} subscriber(s).")


def cmd_alerts(cfg: Config, days: int) -> None:
    from .alerts import clean_name, is_battery_alert
    client, _ = build(cfg)
    sn = resolve_device_sn(client, cfg)
    now = int(time.time())
    alerts = client.device_alerts(sn, now - min(days, 30) * 86400, now)
    if not alerts:
        print(f"No alerts in the last {days} days.")
    for a in sorted(alerts, key=lambda a: a.get("alertStartTime") or 0):
        end = fmt_ts(a["alertEndTime"]) if a.get("alertEndTime") else "active"
        tag = "🔋" if is_battery_alert(a) else "  "
        print(f"{tag} {fmt_ts(a['alertStartTime'])} → {end:16} {clean_name(a.get('alertName'))} "
              f"({a.get('protocolName')}, level {a.get('level')})")


def cmd_alerts_test(cfg: Config) -> None:
    """Send sample alert notifications so you can see how they look."""
    from .alerts import format_alert
    now = int(time.time())
    samples = [
        format_alert({"alertName": "F58 BMS_Comm_Fault", "protocolName": "F58",
                      "alertStartTime": now - 120}, "start"),
        format_alert({"alertName": "F58 BMS_Comm_Fault", "protocolName": "F58",
                      "alertStartTime": now - 1500, "alertEndTime": now}, "end"),
    ]
    # Overload sample, rendered by the real code path with fake readings.
    from .detector import GridState, Reading
    from .monitor import Monitor

    class _Capture:
        def send(self, text):
            samples.append(text)

    class _Mem:
        def __init__(self):
            self.d = {}

        def get(self, k):
            return self.d.get(k)

        def set(self, k, v):
            self.d[k] = v

    Monitor(cfg, None, _Mem(), _Capture()).check_overload(
        Reading(GridState.OFF, now, "", 70, 12000, {"L1": 3700, "L2": 1200, "L3": 900}), now)
    storage = Storage(db_path(cfg))
    notifier = Notifier(cfg.telegram_token, storage)
    for text in samples:
        n = notifier.send("🧪 ТЕСТ\n" + text)
        print(text, f"\n→ sent to {n} subscriber(s)\n")


def cmd_stats(cfg: Config) -> None:
    from .stats import build_stats
    print(build_stats(Storage(db_path(cfg))))


def main() -> None:
    p = argparse.ArgumentParser(prog="deye_notifier", description="Track city grid outages via Deye Cloud")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="poll forever and record outages")
    sub.add_parser("once", help="poll once and print the grid state")
    sub.add_parser("dump", help="print all fields the inverter reports")
    sub.add_parser("stations", help="list stations and devices")
    sub.add_parser("bot", help="run only the Telegram bot (subscribe/unsubscribe)")
    sub.add_parser("telegram-test", help="send a test message to all subscribers")
    al = sub.add_parser("alerts", help="show inverter/battery alert history from Deye Cloud")
    al.add_argument("--days", type=int, default=30)
    sub.add_parser("alerts-test", help="send sample alert notifications to subscribers")
    sub.add_parser("stats", help="print the Telegram statistics message")
    rep = sub.add_parser("report", help="show outage history")
    rep.add_argument("--days", type=int, default=7)
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    cfg = Config.from_env(require_deye=args.cmd in ("run", "once", "dump", "stations", "alerts"))
    if args.cmd == "run":
        cmd_run(cfg)
    elif args.cmd == "once":
        cmd_once(cfg)
    elif args.cmd == "dump":
        cmd_dump(cfg)
    elif args.cmd == "stations":
        cmd_stations(cfg)
    elif args.cmd == "bot":
        cmd_bot(cfg)
    elif args.cmd == "telegram-test":
        cmd_telegram_test(cfg)
    elif args.cmd == "alerts":
        cmd_alerts(cfg, args.days)
    elif args.cmd == "alerts-test":
        cmd_alerts_test(cfg)
    elif args.cmd == "stats":
        cmd_stats(cfg)
    elif args.cmd == "report":
        cmd_report(cfg, args.days)


if __name__ == "__main__":
    main()
