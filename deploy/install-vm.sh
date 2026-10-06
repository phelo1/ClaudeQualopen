#!/usr/bin/env bash
# Idempotent installer for a plain Ubuntu VM. Run as the login user (needs sudo).
#
#   APP_DIR=$HOME/qmag PORT=8855 bash deploy/install-vm.sh
#
# Installs python3 + venv, creates the venv, installs qmag, builds the universe
# if missing, and installs two systemd units:
#   qmag-dashboard.service  -> qmag dashboard on 127.0.0.1:$PORT (reach it over an SSH tunnel)
#   qmag-daemon.service     -> qmag daemon (paper by default; see BROKER)
# Re-running after a code update just reinstalls the package and restarts both units.
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/qmag}"
PORT="${PORT:-8855}"
# Broker: explicit BROKER env wins; otherwise keep whatever the installed unit
# already uses (so a code update never silently switches ibkr back to paper);
# paper on a fresh machine.
if [ -z "${BROKER:-}" ]; then
  BROKER="$(grep -o -- '--broker [a-z-]*' /etc/systemd/system/qmag-daemon.service 2>/dev/null | awk '{print $2}' || true)"
  BROKER="${BROKER:-paper}"
fi
STATE_DIR="${STATE_DIR:-$APP_DIR/paper_state}"
EXTRAS="${EXTRAS:-alpaca}"
RUN_USER="$(id -un)"

cd "$APP_DIR"

if ! command -v python3 >/dev/null || ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "python3 >= 3.11 required" >&2
  exit 1
fi
if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip >/dev/null
fi

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e ".[${EXTRAS}]"
mkdir -p "$STATE_DIR" data

LIVE_FLAG=""
case "$BROKER" in
  *-live)
    if [ "${QMAG_YES_LIVE:-0}" != "1" ]; then
      echo "Refusing to install a LIVE daemon without QMAG_YES_LIVE=1" >&2
      exit 2
    fi
    LIVE_FLAG="--yes-live"
    ;;
esac

sudo tee /etc/systemd/system/qmag-dashboard.service >/dev/null <<EOF
[Unit]
Description=qmag dashboard (Qullamaggie momentum desk)
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

sudo tee /etc/systemd/system/qmag-daemon.service >/dev/null <<EOF
[Unit]
Description=qmag 24/7 trading daemon (Qullamaggie setups)
After=network-online.target qmag-dashboard.service
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
sudo tee /etc/systemd/system/qmag-watchdog.service >/dev/null <<EOF
[Unit]
Description=ClaudeQual scheduler stall recovery
[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/qmag watchdog --state-dir $STATE_DIR --recover
TimeoutStartSec=180
EOF
sudo tee /etc/systemd/system/qmag-watchdog.timer >/dev/null <<EOF
[Unit]
Description=Check ClaudeQual scheduler every minute
[Timer]
OnBootSec=5min
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
EOF
# Narrow permission: the watchdog may restart only this daemon, never Gateway.
SYSTEMCTL=$(command -v systemctl)
printf '%s ALL=(root) NOPASSWD: %s restart qmag-daemon\n' "$RUN_USER" "$SYSTEMCTL" | sudo tee /etc/sudoers.d/qmag-watchdog >/dev/null
sudo chmod 440 /etc/sudoers.d/qmag-watchdog
sudo visudo -cf /etc/sudoers.d/qmag-watchdog
sudo systemctl daemon-reload
sudo systemctl enable -q --now qmag-watchdog.timer
sudo systemctl enable -q qmag-dashboard qmag-daemon
sudo systemctl restart qmag-dashboard

if [ ! -f universe/market.txt ]; then
  echo "No universe yet; building the whole-market universe (takes a while)..."
  .venv/bin/qmag universe build || echo "Universe build failed; the daemon rebuilds it on Sunday and falls back to universe/default.txt meanwhile" >&2
fi

sudo systemctl restart qmag-daemon
sleep 2
systemctl --no-pager --lines=0 status qmag-dashboard qmag-daemon || true
echo
echo "Dashboard: http://127.0.0.1:$PORT  (tunnel: ssh -L $PORT:127.0.0.1:$PORT <user>@<host>)"
echo "Logs:      journalctl -u qmag-dashboard -u qmag-daemon -f"
