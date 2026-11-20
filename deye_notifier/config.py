"""Configuration loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments. Existing env wins."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class Config:
    app_id: str
    app_secret: str
    email: str
    password: str
    base_url: str
    station_id: str | None
    device_sn: str | None
    poll_interval: int
    grid_voltage_threshold: float
    grid_voltage_keys: tuple[str, ...]
    stale_after: int
    battery_low_soc: float
    overload_percent: float
    alerts_enabled: bool
    alert_ignore: tuple[str, ...]
    data_dir: Path
    telegram_token: str | None
    battery_temp_max: float = 45
    battery_temp_min: float = 5
    inverter_temp_max: float = 70
    not_charging_minutes: int = 30
    not_charging_below_soc: float = 95

    @classmethod
    def from_env(cls, env_file: Path | None = None, require_deye: bool = True) -> "Config":
        load_dotenv(env_file or Path.cwd() / ".env")
        if os.environ.get("TZ") and hasattr(time, "tzset"):
            time.tzset()  # apply TZ from .env (e.g. Europe/Kyiv) to all timestamps

        def req(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value and require_deye:
                raise SystemExit(f"Missing required setting {name} (see .env.example)")
            return value

        def opt(name: str) -> str | None:
            value = os.environ.get(name, "").strip()
            return value or None

        keys = opt("GRID_VOLTAGE_KEYS")
        return cls(
            app_id=req("DEYE_APP_ID"),
            app_secret=req("DEYE_APP_SECRET"),
            email=req("DEYE_EMAIL"),
            password=req("DEYE_PASSWORD"),
            base_url=(opt("DEYE_BASE_URL") or "https://eu1-developer.deyecloud.com/v1.0").rstrip("/"),
            station_id=opt("DEYE_STATION_ID"),
            device_sn=opt("DEYE_DEVICE_SN"),
            poll_interval=int(opt("POLL_INTERVAL") or 120),
            grid_voltage_threshold=float(opt("GRID_VOLTAGE_THRESHOLD") or 100),
            grid_voltage_keys=tuple(k.strip() for k in keys.split(",") if k.strip()) if keys else (),
            stale_after=int(opt("STALE_AFTER") or 900),
            battery_low_soc=float(opt("BATTERY_LOW_SOC") or 45),
            overload_percent=float(opt("OVERLOAD_PERCENT") or 80),
            alerts_enabled=(opt("ALERTS_ENABLED") or "1").lower() not in ("0", "false", "no", "off"),
            alert_ignore=tuple(
                x.strip() for x in (os.environ.get("ALERT_IGNORE", "Grid_Phase_Wrong")).split(",")
                if x.strip()
            ),
            data_dir=Path(opt("DATA_DIR") or "data"),
            telegram_token=opt("TELEGRAM_BOT_TOKEN"),
            battery_temp_max=float(opt("BATTERY_TEMP_MAX") or 45),
            battery_temp_min=float(opt("BATTERY_TEMP_MIN") or 5),
            inverter_temp_max=float(opt("INVERTER_TEMP_MAX") or 70),
            not_charging_minutes=int(opt("NOT_CHARGING_MINUTES") or 30),
            not_charging_below_soc=float(opt("NOT_CHARGING_BELOW_SOC") or 95),
        )
