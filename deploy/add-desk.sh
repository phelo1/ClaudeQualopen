#!/usr/bin/env bash
# Add a second (third, ...) trading desk on a machine installed with deploy/install-vm.sh.
#
#   bash deploy/add-desk.sh NAME --broker alpaca [--port 8856] [--dir ~/qmag] [--main-state ~/qmag/paper_state]
#
# One desk = one broker account: its own state directory (journal, settings,
# credentials, kill switch), its own daemon and its own dashboard port. This
# script creates $APP_DIR/desks/NAME, installs qmag-daemon-NAME.service and
# qmag-dashboard-NAME.service, starts the new desk's strategy from the main
# desk's settings.yaml (if any), and lists the new desk in the main desk's
# QMAG_DESKS so the main dashboard's accounts page shows it (and the new
# desk's page shows the main one). Broker credentials are NOT copied: put
# that account's keys on the new desk's settings page (http://127.0.0.1:PORT
# over an SSH tunnel) or in desks/NAME/settings.env.
#
# Live brokers (*-live) need QMAG_YES_LIVE=1, exactly like install-vm.sh.
set -euo pipefail

usage() { echo "usage: bash deploy/add-desk.sh NAME --broker paper|alpaca|ibkr|mt5|alpaca-live|ibkr-live|mt5-live [--port N] [--dir PATH] [--main-state PATH]" >&2; exit 1; }

NAME="${1:-}"; [ -n "$NAME" ] || usage
shift
case "$NAME" in -*) usage ;; esac
if ! [[ "$NAME" =~ ^[a-z0-9][a-z0-9_-]{0,31}$ ]]; then
  echo "NAME must be lowercase letters, digits, - or _ (max 32 chars)" >&2; exit 1
fi

BROKER=""; PORT=""; APP_DIR="${APP_DIR:-$HOME/qmag}"; MAIN_STATE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --broker) BROKER="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --dir) APP_DIR="$2"; shift 2 ;;
    --main-state) MAIN_STATE="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; usage ;;
  esac
done
[ -n "$BROKER" ] || usage
MAIN_STATE="${MAIN_STATE:-$APP_DIR/paper_state}"
STATE_DIR="$APP_DIR/desks/$NAME"
RUN_USER="$(id -un)"

cd "$APP_DIR"
[ -x .venv/bin/qmag ] || { echo "no qmag install in $APP_DIR (run deploy/install-vm.sh first)" >&2; exit 1; }

LIVE_FLAG=""
case "$BROKER" in
  *-live)
    if [ "${QMAG_YES_LIVE:-0}" != "1" ]; then
      echo "Refusing to install a LIVE desk without QMAG_YES_LIVE=1" >&2
      exit 2
    fi
    LIVE_FLAG="--yes-live"
    ;;
esac

# Pick a free dashboard port when none was given: the main desk's port + 1, + 2, ...
if [ -z "$PORT" ]; then
  MAIN_PORT="$(grep -o -- '--port [0-9]*' /etc/systemd/system/qmag-dashboard.service 2>/dev/null | awk '{print $2}' || true)"
  PORT=$(( ${MAIN_PORT:-8855} + 1 ))
  while grep -qs -- "--port $PORT " /etc/systemd/system/qmag-dashboard*.service; do PORT=$(( PORT + 1 )); done
fi

mkdir -p "$STATE_DIR"
if [ -f "$MAIN_STATE/settings.yaml" ] && [ ! -f "$STATE_DIR/settings.yaml" ]; then
  cp "$MAIN_STATE/settings.yaml" "$STATE_DIR/settings.yaml"
  echo "Strategy settings copied from $MAIN_STATE/settings.yaml (edit them on the new desk's settings page)"
fi

# settings.env edits go through qmag's own store so quoting and the 0600 mode are right.
.venv/bin/python - "$NAME" "$STATE_DIR" "$MAIN_STATE" "$PORT" "$BROKER" <<'PY'
import sys
from pathlib import Path
from qmag.settings import SettingsStore

name, state_dir, main_state, port, broker = sys.argv[1:6]

def add_desk(store: SettingsStore, entry_name: str, path: str, url: str) -> None:
    values = store.load_env()
    entries = [e.strip() for e in values.get("QMAG_DESKS", "").replace(";", ",").split(",") if e.strip()]
    entries = [e for e in entries if not e.startswith(entry_name + "=")]
    entries.append(f"{entry_name}={path}|{url}")
    values["QMAG_DESKS"] = ",".join(entries)
    store.save_env(values)

new = SettingsStore(state_dir)
values = new.load_env()
values.setdefault("QMAG_DESK_NAME", name)
new.save_env(values)

main = SettingsStore(main_state)
main_port = "8855"
try:
    import re
    unit = Path("/etc/systemd/system/qmag-dashboard.service").read_text()
    m = re.search(r"--port (\d+)", unit)
    if m:
        main_port = m.group(1)
except OSError:
    pass
main_name = main.load_env().get("QMAG_DESK_NAME") or Path(main_state).name
add_desk(main, name, state_dir, f"http://127.0.0.1:{port}")
add_desk(new, main_name, main_state, f"http://127.0.0.1:{main_port}")
print(f"{main_state}/settings.env: QMAG_DESKS now lists {name} -> {state_dir}")
print(f"{state_dir}/settings.env: QMAG_DESK_NAME={name}; QMAG_DESKS lists {main_name}")
PY

sudo tee "/etc/systemd/system/qmag-dashboard-$NAME.service" >/dev/null <<EOF
[Unit]
Description=qmag dashboard for desk $NAME ($BROKER)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=-$APP_DIR/.env
Environment=TZ=America/New_York
ExecStart=$APP_DIR/.venv/bin/qmag dashboard --host 127.0.0.1 --port $PORT --broker $BROKER --state-dir $STATE_DIR
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo tee "/etc/systemd/system/qmag-daemon-$NAME.service" >/dev/null <<EOF
[Unit]
Description=qmag trading daemon for desk $NAME ($BROKER)
After=network-online.target qmag-dashboard-$NAME.service
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=-$APP_DIR/.env
Environment=TZ=America/New_York
ExecStart=$APP_DIR/.venv/bin/qmag daemon --broker $BROKER --state-dir $STATE_DIR $LIVE_FLAG
Restart=always
RestartSec=30
KillSignal=SIGINT
TimeoutStopSec=120
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable -q "qmag-dashboard-$NAME" "qmag-daemon-$NAME"
sudo systemctl restart "qmag-dashboard-$NAME" "qmag-daemon-$NAME"
sleep 2
systemctl --no-pager --lines=0 status "qmag-dashboard-$NAME" "qmag-daemon-$NAME" || true

cat <<EOF

Desk '$NAME' ($BROKER) is running.
  state:      $STATE_DIR
  dashboard:  http://127.0.0.1:$PORT   (tunnel: ssh -L $PORT:127.0.0.1:$PORT $RUN_USER@<host>)
  logs:       journalctl -u qmag-daemon-$NAME -u qmag-dashboard-$NAME -f
  accounts:   the main dashboard's /accounts page lists it after its first cycle (or: qmag accounts --state-dir $MAIN_STATE)

Next:
  1. Put this account's credentials on the new desk's settings page (or in $STATE_DIR/settings.env):
       alpaca: ALPACA_API_KEY / ALPACA_SECRET_KEY for THAT account
       ibkr:   a second IB login needs its own gateway (copy deploy/ibgateway to another directory with other
               host ports, e.g. 4011/4012) and IBKR_PORT / IBKR_CLIENT_ID / IBKR_DATA_CLIENT_ID that differ from the main desk
       mt5:    MT5_LOGIN / MT5_PASSWORD / MT5_SERVER
  2. Check it: $APP_DIR/.venv/bin/qmag broker-test --broker $BROKER --state-dir $STATE_DIR
  3. Watch-only account? Throw its kill switch: $APP_DIR/.venv/bin/qmag halt --broker $BROKER --state-dir $STATE_DIR -r "watch only"
Remove it later: sudo systemctl disable --now qmag-daemon-$NAME qmag-dashboard-$NAME; sudo rm /etc/systemd/system/qmag-*-$NAME.service; sudo systemctl daemon-reload
EOF
