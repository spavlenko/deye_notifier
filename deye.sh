#!/usr/bin/env bash
# Control the Deye notifier (grid monitor + Telegram bot) in the background.
#
#   ./deye.sh start | stop | restart | status | logs | tail
#
# Logs: data/monitor.log (rotated at 10 MB). PID: data/deye.pid
# On macOS, `caffeinate` keeps the Mac from idle-sleeping while the notifier runs.

set -euo pipefail

cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
DATA_DIR="data"
PID_FILE="$DATA_DIR/deye.pid"
LOG_FILE="$DATA_DIR/monitor.log"
MAX_LOG_BYTES=$((10 * 1024 * 1024))
PATTERN="-m deye_notifier run"

mkdir -p "$DATA_DIR"

pid() {
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    cat "$PID_FILE"
    return
  fi
  # Fallback: an instance started by hand without the PID file.
  pgrep -f -- "$PATTERN" | head -1 || true
}

rotate_log() {
  if [[ -f "$LOG_FILE" ]] && (( $(wc -c <"$LOG_FILE") > MAX_LOG_BYTES )); then
    mv -f "$LOG_FILE" "$LOG_FILE.1"
  fi
}

start() {
  local p
  p="$(pid)"
  if [[ -n "$p" ]]; then
    echo "Already running (PID $p)"
    return
  fi
  if [[ ! -f .env ]]; then
    echo "No .env file. Copy .env.example to .env and fill it in." >&2
    exit 1
  fi
  rotate_log
  PYTHONUNBUFFERED=1 nohup "$PYTHON" -m deye_notifier run >>"$LOG_FILE" 2>&1 &
  p=$!
  echo "$p" >"$PID_FILE"
  if command -v caffeinate >/dev/null; then
    # Exits by itself when the notifier stops.
    nohup caffeinate -i -w "$p" >/dev/null 2>&1 &
  fi
  sleep 3
  if kill -0 "$p" 2>/dev/null; then
    echo "Started (PID $p). Log: $LOG_FILE"
    tail -n 5 "$LOG_FILE"
  else
    echo "Failed to start. Last log lines:" >&2
    tail -n 20 "$LOG_FILE" >&2
    rm -f "$PID_FILE"
    exit 1
  fi
}

stop() {
  local p
  p="$(pid)"
  if [[ -z "$p" ]]; then
    echo "Not running"
    rm -f "$PID_FILE"
    return
  fi
  kill "$p"
  for _ in $(seq 1 20); do
    kill -0 "$p" 2>/dev/null || break
    sleep 0.5
  done
  if kill -0 "$p" 2>/dev/null; then
    echo "Did not stop in 10 s, killing"
    kill -9 "$p"
  fi
  rm -f "$PID_FILE"
  echo "Stopped (PID $p)"
}

status() {
  local p
  p="$(pid)"
  if [[ -n "$p" ]]; then
    echo "Running (PID $p, since $(ps -o lstart= -p "$p"))"
    tail -n 3 "$LOG_FILE" 2>/dev/null || true
  else
    echo "Not running"
    exit 1
  fi
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  logs) tail -n "${2:-50}" "$LOG_FILE" ;;
  tail) tail -f "$LOG_FILE" ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|logs [N]|tail}" >&2
    exit 2
    ;;
esac
