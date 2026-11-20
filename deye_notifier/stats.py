"""Text statistics for the Telegram «📊 Статистика» button (Ukrainian)."""

from __future__ import annotations

import time
from datetime import datetime

from .storage import Storage

# Ukrainian standard (DSTU EN 50160): 230 V ±10%
V_LOW, V_HIGH = 207, 253


def plural_uk(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def dur_uk(seconds: int) -> str:
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} д {h} год"
    return f"{h} год {m} хв" if h else f"{m} хв"


def _ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")


def outage_summary(storage: Storage, since: int, now: int) -> tuple[int, int, int]:
    """(count, total seconds without grid, longest) for outages overlapping [since, now]."""
    count = total = longest = 0
    for o in storage.outages(since):
        start = max(o.started_at, since)
        end = min(o.ended_at or now, now)
        if end <= start and o.ended_at is not None:
            continue
        count += 1
        total += max(0, end - start)
        longest = max(longest, (o.ended_at or now) - o.started_at)
    return count, total, longest


def _times(n: int) -> str:
    return f"{n} {plural_uk(n, 'раз', 'рази', 'разів')}"


def current_status(storage: Storage, now: int) -> list[str]:
    lines = []
    open_outage = storage.open_outage()
    state = storage.get("grid_state")
    if open_outage:
        lines.append(f"Зараз: 💡❌ світла немає {dur_uk(now - open_outage.started_at)} "
                     f"(з {_ts(open_outage.started_at)})")
    elif state == "on":
        last = storage.outages()
        since = f" (з {_ts(last[-1].ended_at)})" if last and last[-1].ended_at else ""
        lines.append(f"Зараз: 💡 світло є{since}")
    else:
        lines.append("Зараз: стан мережі невідомий")

    r = storage.latest_reading()
    if r:
        parts = []
        if r["soc"] is not None:
            parts.append(f"🔋 {r['soc']:g}%")
        if r["load_w"] is not None:
            parts.append(f"🏠 {r['load_w'] / 1000:.1f} кВт")
        if r["pv_w"]:
            parts.append(f"☀️ {r['pv_w'] / 1000:.1f} кВт")
        if parts:
            lines.append("  ".join(parts) + f"  ({_ts(r['ts'])})")
    return lines


def grid_quality(storage: Storage, since: int) -> list[str]:
    rows = [r for r in storage.readings(since) if r["grid_state"] == "on"]
    lines = []
    if rows:
        out_of_range = 0
        for col, ph in (("grid_v1", "L1"), ("grid_v2", "L2"), ("grid_v3", "L3")):
            vals = [r[col] for r in rows if r[col] is not None]
            if not vals:
                continue
            avg = sum(vals) / len(vals)
            lines.append(f"• {ph}: {min(vals):.0f}–{max(vals):.0f} В, середня {avg:.0f} В")
        for r in rows:
            vs = [r[c] for c in ("grid_v1", "grid_v2", "grid_v3") if r[c] is not None]
            if any(v < V_LOW or v > V_HIGH for v in vs):
                out_of_range += 1
        pct = out_of_range / len(rows) * 100
        lines.append(f"• Поза нормою ({V_LOW}–{V_HIGH} В): {pct:.0f}% часу")
    else:
        lines.append("• Ще немає даних — статистика накопичується")
    phase = storage.count_alerts("Grid_Phase_Wrong", since)
    lines.append(f"• Збої фаз (Grid_Phase_Wrong): {_times(phase)}")
    return lines


def build_stats(storage: Storage, now: int | None = None) -> str:
    now = int(now or time.time())
    midnight = int(datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0).timestamp())
    lines = ["📊 Статистика", ""]
    lines += current_status(storage, now)

    lines += ["", "🔌 Відключення:"]
    for label, since in (("Сьогодні", midnight), ("7 днів", now - 7 * 86400),
                         ("30 днів", now - 30 * 86400)):
        count, total, longest = outage_summary(storage, since, now)
        if not count:
            lines.append(f"• {label}: без відключень")
            continue
        line = f"• {label}: {_times(count)}, {dur_uk(total)}"
        if count > 1:
            line += f" (найдовше {dur_uk(longest)})"
        lines.append(line)
    _, week_off, _ = outage_summary(storage, now - 7 * 86400, now)
    lines.append(f"• Без світла за 7 днів: {week_off / (7 * 86400) * 100:.0f}% часу")

    lines += ["", "⚡ Якість мережі (7 днів):"]
    lines += grid_quality(storage, now - 7 * 86400)
    return "\n".join(lines)
