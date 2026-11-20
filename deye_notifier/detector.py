"""Decide from inverter data whether the city grid is present."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class GridState(str, Enum):
    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


@dataclass
class Reading:
    state: GridState
    collected_at: int | None  # unix seconds from the inverter
    detail: str
    soc: float | None = None  # battery state of charge, %
    rated_power: float | None = None  # inverter rated power, W
    load_phases: dict[str, float] | None = None  # {"L1": W, ...}
    metrics: dict[str, float | None] | None = None  # values saved to reading history


# Keys Deye uses for grid-side voltage differ between inverter models/firmware.
# We match anything that looks like a grid voltage, but skip load/inverter/PV sides.
_GRID_VOLT_RE = re.compile(r"grid.*volt|volt.*grid|^ac.?voltage", re.I)
_EXCLUDE_RE = re.compile(r"load|inverter|pv|dc|gen|ups|battery|bms|freq|power|current", re.I)


def flatten_data_list(device: dict[str, Any]) -> dict[str, tuple[Any, str]]:
    """deviceDataList item -> {key: (value, unit)}."""
    out: dict[str, tuple[Any, str]] = {}
    for item in device.get("dataList") or []:
        key = item.get("key")
        if key:
            out[key] = (item.get("value"), item.get("unit") or "")
    return out


def find_grid_voltage_keys(data: dict[str, tuple[Any, str]]) -> list[str]:
    keys = [k for k in data if _GRID_VOLT_RE.search(k) and not _EXCLUDE_RE.search(k.replace("Voltage", ""))]
    # Prefer explicit grid-side fields (e.g. GridVoltageL1). Generic "AC Voltage" fields
    # can be the inverter's output, which stays ~230V on battery during an outage.
    explicit = [k for k in keys if "grid" in k.lower()]
    return explicit or keys


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _collected_at(device: dict[str, Any]) -> int | None:
    ts = device.get("collectionTime")
    ts = _to_float(ts)
    if ts is None:
        return None
    return int(ts / 1000) if ts > 1e11 else int(ts)  # ms or s


# GridRelayStatus value(s) meaning "inverter is connected to the city grid".
# Only "Pull-in" has been observed so far; any other value is treated as disconnected.
RELAY_KEY = "GridRelayStatus"
STATUS_KEY = "GridStatus"
RELAY_CONNECTED = ("pull-in",)


def load_info(device: dict[str, Any] | None) -> tuple[float | None, dict[str, float]]:
    """(rated power W, per-phase load W) from LoadPowerL1..L3 / RatedPower."""
    data = flatten_data_list(device or {})
    rated = _to_float(data["RatedPower"][0]) if "RatedPower" in data else None
    phases: dict[str, float] = {}
    for n in (1, 2, 3):
        key = f"LoadPowerL{n}"
        if key in data and (v := _to_float(data[key][0])) is not None:
            phases[f"L{n}"] = v
    return rated, phases


# history column -> candidate inverter keys (first present wins)
METRIC_KEYS: dict[str, tuple[str, ...]] = {
    "grid_v1": ("GridVoltageL1",),
    "grid_v2": ("GridVoltageL2",),
    "grid_v3": ("GridVoltageL3",),
    "load_w": ("TotalConsumptionPower", "UPSLoadPower"),
    "pv_w": ("TotalSolarPower",),
    "grid_w": ("TotalGridPower",),
    "battery_w": ("BatteryPower",),
    "battery_temp": ("Temperature- Battery", "BatteryTemperature", "BMSTemperature"),
    "inverter_temp": ("AC Temperature", "DC Temperature"),
}


def extract_metrics(device: dict[str, Any] | None) -> dict[str, float | None]:
    data = flatten_data_list(device or {})
    out: dict[str, float | None] = {}
    for col, candidates in METRIC_KEYS.items():
        out[col] = next(
            (v for k in candidates if k in data and (v := _to_float(data[k][0])) is not None), None
        )
    if not out["pv_w"]:
        pv = [_to_float(data[k][0]) for k in data if k.startswith("DCPowerPV")]
        if any(v is not None for v in pv):
            out["pv_w"] = sum(v or 0 for v in pv)
    return out


def battery_soc(device: dict[str, Any] | None) -> float | None:
    data = flatten_data_list(device or {})
    for key in ("SOC", "BMSSOC"):
        if key in data and (v := _to_float(data[key][0])) is not None:
            return v
    return None


def detect(
    device: dict[str, Any] | None,
    threshold: float,
    keys: tuple[str, ...] = (),
) -> Reading:
    reading = _detect_grid(device, threshold, keys)
    reading.soc = battery_soc(device)
    reading.rated_power, reading.load_phases = load_info(device)
    reading.metrics = extract_metrics(device)
    return reading


def _detect_grid(
    device: dict[str, Any] | None,
    threshold: float,
    keys: tuple[str, ...] = (),
) -> Reading:
    """Decide grid presence from grid-side voltage, falling back to the relay status.

    - any grid voltage at/above threshold        -> ON, even if the relay is open
      (the inverter drops the relay when voltage/frequency is out of its allowed range,
      but the city grid is still there)
    - grid voltages present, all below threshold -> OFF (no voltage = no grid)
    - no voltage fields: GridRelayStatus "Pull-in" -> ON, anything else -> OFF
    """
    if not device:
        return Reading(GridState.UNKNOWN, None, "no device data")

    data = flatten_data_list(device)
    collected = _collected_at(device)

    voltage_keys = list(keys) or find_grid_voltage_keys(data)
    voltages = {k: _to_float(data[k][0]) for k in voltage_keys if k in data}
    voltages = {k: v for k, v in voltages.items() if v is not None}

    relay = str(data[RELAY_KEY][0]).strip() if RELAY_KEY in data else None
    status = str(data[STATUS_KEY][0]).strip() if STATUS_KEY in data else None

    parts = []
    if relay is not None:
        parts.append(f"{RELAY_KEY}={relay}")
    if status is not None:
        parts.append(f"{STATUS_KEY}={status}")
    parts += [f"{k}={v:g}V" for k, v in voltages.items()]
    detail = ", ".join(parts)

    if relay is None and not voltages:
        return Reading(
            GridState.UNKNOWN,
            collected,
            "no GridRelayStatus or grid voltage field found; run `python -m deye_notifier dump` "
            "and set GRID_VOLTAGE_KEYS",
        )

    if voltages:
        on = max(voltages.values()) >= threshold
    else:
        on = relay.lower() in RELAY_CONNECTED
    return Reading(GridState.ON if on else GridState.OFF, collected, detail)
