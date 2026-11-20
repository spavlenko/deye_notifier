# deye_notifier

Polls the [Deye Cloud OpenAPI](https://developer.deyecloud.com) and tracks when the city grid is unavailable.
It reads the grid-side voltage from your inverter, records each outage (start, end, duration) in SQLite, and can send notifications to Telegram.

It has no dependencies beyond Python 3.10+.

## Setup

1. Sign in at https://developer.deyecloud.com/app with your Deye Cloud account and create an app. This gives you an **App ID** and an **App Secret**.
2. `cp .env.example .env` and fill in `DEYE_APP_ID`, `DEYE_APP_SECRET`, `DEYE_EMAIL` and `DEYE_PASSWORD`.
3. Check that it works:

```sh
python3 -m deye_notifier stations   # lists your stations and inverter serial numbers
python3 -m deye_notifier dump       # every field the inverter reports, plus detected grid-voltage keys
python3 -m deye_notifier once       # current grid state
```

If `dump` reports `Auto-detected grid voltage keys: none`, find the grid voltage field(s) in the list and put them in `.env`, for example `GRID_VOLTAGE_KEYS=GridVoltageL1,GridVoltageL2,GridVoltageL3`.

## Run

```sh
python3 -m deye_notifier run          # poll forever (Ctrl+C to stop)
python3 -m deye_notifier report --days 30
```

Sample `report` output:

```
2026-11-18 07:02  →  2026-11-18 11:15     4h 13m
2026-11-19 18:30  →  ongoing              1h 05m

2 outage(s), 5h 18m without grid in the last 30 days.
```

Set `TZ` in `.env` (default in the example: `Europe/Kyiv`) so alert times and «Сьогодні» in statistics use local time, even on a UTC server.

### Background service

Use `deye.sh` to run the monitor and bot in the background with `nohup`:

```bash
./deye.sh start      # start in background
./deye.sh stop
./deye.sh restart    # after code or .env changes
./deye.sh status
./deye.sh logs 100   # last 100 log lines
./deye.sh tail       # follow the log
```

The log is `data/monitor.log`, rotated at 10 MB. On macOS the script also runs `caffeinate`, so the Mac doesn't idle-sleep while the notifier is running. Closing the lid still puts it to sleep.

## How detection works

- `device/latest` returns the inverter's live data. The grid is **OFF** if either of these is true:
  - `GridRelayStatus` is anything other than `Pull-in`, meaning the inverter has disconnected from the grid and the house runs on battery.
  - All grid-side voltages (`GridVoltageL1..L3`) are below `GRID_VOLTAGE_THRESHOLD` (default 100 V).

  Otherwise it is **ON**. `GridStatus` (e.g. `Purchasing energy`) is written to the log for reference.
- Grid power (`wirePower`) is *not* used, because 0 W also happens when the grid is present but idle.
- Each new data point (by `collectionTime`) is processed once. An outage starts at the first OFF reading and ends at the next ON reading. Timestamps come from the inverter, not from the poll.
- The battery alert fires once when `SOC` drops below `BATTERY_LOW_SOC` (default 45%), then re-arms when the charge is back to that value + 5%.
- If the data is older than `STALE_AFTER`, the state is treated as **unknown**. You get one "stale" notice, and no outage is recorded or closed. This covers the case where your router or dongle loses power along with the grid. With no battery or UPS on the router, the cloud receives no data during an outage. You'll then see a stale notice, and the outage is recorded only once data resumes and still shows the grid OFF.

## Overload warning

While the grid is down, the inverter carries the whole house from the battery. If any phase reaches `OVERLOAD_PERCENT` (default 80%) of its capacity, you get a warning. The capacity per phase is `RatedPower / 3`, which is 4 kW for a 12 kW inverter. The same applies when the total load reaches 80% of `RatedPower`. The warning re-arms once the load drops below 70%. When the grid is on, heavy loads draw from the grid, so no warning is sent.

## Battery and temperature alerts

- **Battery not charging:** the grid has been on for `NOT_CHARGING_MINUTES` (default 30), the battery is below `NOT_CHARGING_BELOW_SOC` (default 95%), and SOC hasn't risen. The alert re-arms once SOC rises 2% or the grid goes down.
- **Battery temperature:** alerts at or above `BATTERY_TEMP_MAX` (default 45 °C) and at or below `BATTERY_TEMP_MIN` (default 5 °C; LiFePO4 shouldn't charge near 0 °C).
- **Inverter temperature:** alerts at or above `INVERTER_TEMP_MAX` (default 70 °C).
- Temperature alerts send a «в нормі» message once the value is 3 °C back inside the range.

## Inverter and battery alerts

Each poll also reads `/device/alertList`, the inverter's fault and warning list from Deye Cloud. That list includes battery and BMS faults. Each new alert sends a message, and so does its resolution. Battery and BMS alerts are marked 🔋.

- On the first run, existing alerts are recorded silently, so old history doesn't spam you.
- `ALERT_IGNORE` (default `Grid_Phase_Wrong`) lists alert names to skip. Set it empty to get every alert.
- `python3 -m deye_notifier alerts --days 30` shows the alert history (the API allows at most 30 days).
- `python3 -m deye_notifier alerts-test` sends sample alert messages to subscribers.

## Telegram bot (optional)

The bot gives everyone who opens it two buttons: **🔔 Підписатися** and **🔕 Відписатися**. Outage notifications go to every subscriber. Groups work too: add the bot to a group and send `/subscribe` there.

1. In Telegram, message @BotFather, send `/newbot`, and copy the token into `TELEGRAM_BOT_TOKEN` in `.env`.
2. Start the bot. `run` starts the bot together with the monitor. `bot` runs only the bot, which is handy for testing before your Deye credentials are set.
   ```sh
   python3 -m deye_notifier bot
   ```
3. Open your bot in Telegram, press **Start**, then **🔔 Підписатися**.
4. `python3 -m deye_notifier telegram-test` sends a test message to all subscribers.

The bot uses inline buttons (under the message): **📊 Статистика**, **🔔 Підписатися / 🔕 Відписатися**; in stats view also **🔄 Оновити** and **⬅️ Меню**. Commands: `/menu`, `/stats`, `/subscribe`, `/unsubscribe`.

**Inline mode:** type `@your_bot` in any chat and pick «📊 Статистика» (full stats) or «💡 Зараз» (current state only) to post it there. Enable inline mode in @BotFather → /setinline.

**Groups:** add the bot to a group and it posts the menu there. The group itself becomes the subscriber; only group admins can subscribe/unsubscribe it, everyone can view stats. Removing the bot from the group unsubscribes it.

The **📊 Статистика** button (or `/stats`) shows the current status, outages for today, 7 days and 30 days, and grid quality. Grid quality covers voltage per phase, the share of time outside 207–253 V, and phase faults. Preview it with `python3 -m deye_notifier stats`.

Every poll is saved to the `readings` table and kept for 120 days. Subscribers are stored in `data/deye.sqlite3`. Anyone who blocks the bot is removed automatically.

Messages (in Ukrainian):

```
💡❌ Світло зникло
🕒 2026-11-20 18:30

💡✅ Світло з'явилося
🕒 2026-11-20 22:43
⏱ Без світла: 4 год 13 хв (з 2026-11-20 18:30)
```

## Tests

```sh
python3 -m unittest discover -s tests -t .
```
