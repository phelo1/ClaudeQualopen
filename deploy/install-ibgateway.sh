#!/usr/bin/env bash
# Install Docker (if missing) and run IB Gateway headless next to qmag.
#
#   bash deploy/install-ibgateway.sh              # first run: creates ~/ibgateway/.env for you to fill in, then exits
#   bash deploy/install-ibgateway.sh              # second run (after filling .env): starts the gateway
#
# Afterwards, wire qmag to it:
#   bash deploy/install-ibgateway.sh --wire       # saves IBKR_* into the desk settings, installs ib_async,
#                                                 # switches the systemd units to --broker ibkr (IBKR PAPER) and restarts them
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/qmag}"
GW_DIR="${GW_DIR:-$HOME/ibgateway}"
PORT="${PORT:-8855}"
STATE_DIR="${STATE_DIR:-$APP_DIR/paper_state}"
WIRE=0
[ "${1:-}" = "--wire" ] && WIRE=1

if [ "$WIRE" = 1 ]; then
  cd "$APP_DIR"
  .venv/bin/pip install -q "ib_async>=1.0"
  .venv/bin/python - <<EOF
from qmag.settings import SettingsStore
s = SettingsStore("$STATE_DIR")
v = s.load_env()
v.update({"IBKR_HOST": "127.0.0.1", "IBKR_PORT": "4002", "IBKR_CLIENT_ID": "17", "IBKR_DATA_CLIENT_ID": "18"})
print("saved IBKR_* to", s.save_env(v))
EOF
  echo "Probing the gateway port..."
  if ! (exec 3<>/dev/tcp/127.0.0.1/4002) 2>/dev/null; then
    echo "Nothing is listening on 127.0.0.1:4002 - is the gateway container up and logged in? (cd $GW_DIR && docker compose logs --tail 50)" >&2
    exit 3
  fi
  .venv/bin/qmag status --probe --broker ibkr --state-dir "$STATE_DIR" || true
  .venv/bin/qmag broker-test --broker ibkr --kind bracket --state-dir "$STATE_DIR"
  APP_DIR="$APP_DIR" PORT="$PORT" BROKER=ibkr EXTRAS=alpaca,ibkr bash "$APP_DIR/deploy/install-vm.sh"
  exit 0
fi

if ! command -v docker >/dev/null; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-v2 >/dev/null
  sudo systemctl enable -q --now docker
  sudo usermod -aG docker "$(id -un)"
  echo "Docker installed. Your user was added to the docker group; re-run this script in a NEW shell (or prefix docker commands with sudo)."
fi

mkdir -p "$GW_DIR"
cp "$APP_DIR/deploy/ibgateway/docker-compose.yml" "$GW_DIR/docker-compose.yml"
if [ ! -f "$GW_DIR/.env" ]; then
  cp "$APP_DIR/deploy/ibgateway/.env.example" "$GW_DIR/.env"
  chmod 600 "$GW_DIR/.env"
  touch "$GW_DIR/tws_password" && chmod 600 "$GW_DIR/tws_password"
  echo "Created $GW_DIR/.env and $GW_DIR/tws_password - put the PAPER username in TWS_USERID and the password in the tws_password file, then run this script again."
  exit 0
fi
if ! grep -q '^TWS_USERID=.\+' "$GW_DIR/.env" || [ ! -s "$GW_DIR/tws_password" ]; then
  echo "$GW_DIR/.env has no TWS_USERID or $GW_DIR/tws_password is empty. Fill them in and run again." >&2
  exit 2
fi
chmod 600 "$GW_DIR/.env"

cd "$GW_DIR"
DOCKER=docker
docker info >/dev/null 2>&1 || DOCKER="sudo docker"
IMAGE="$(grep -m1 'image:' docker-compose.yml | awk '{print $2}')"
PROJECT="$(grep -m1 '^name:' docker-compose.yml | awk '{print $2}')"
$DOCKER compose pull -q

# The image runs as an unprivileged user (uid 1000 today); the host user is
# often uid 1001+ on cloud images. Both the mounted password file and the
# settings volume must be readable/writable by the *container's* uid or the
# gateway restarts forever with "Permission denied".
GW_UID="$($DOCKER run --rm --entrypoint id "$IMAGE" -u 2>/dev/null || echo 1000)"
sudo chown "$GW_UID" "$GW_DIR/tws_password" && sudo chmod 0400 "$GW_DIR/tws_password"
$DOCKER volume create "${PROJECT}_tws_settings" >/dev/null
$DOCKER run --rm --user 0 --entrypoint sh -v "${PROJECT}_tws_settings:/settings" "$IMAGE" -c "chown -R $GW_UID:$GW_UID /settings"

wait_login() {  # wait up to ~4 min for IBC to report a completed login
  local since="$1" i
  for i in $(seq 1 24); do
    sleep 10
    if $DOCKER compose logs --no-color --since "$since" 2>/dev/null | grep -q "Login has completed"; then return 0; fi
  done
  return 1
}

STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
$DOCKER compose up -d
echo "Gateway starting; waiting for the login (2FA prompts show up in: cd $GW_DIR && $DOCKER compose logs -f)..."
if wait_login "$STAMP"; then
  echo "Login completed."
  if [ ! -f "$GW_DIR/.settings-initialised" ]; then
    # IBC unticks "Read-Only API" through the settings dialog on first login,
    # but the gateway only honours it after a restart; do that once now.
    echo "First login: restarting once so the Read-Only API setting takes effect..."
    STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    $DOCKER compose restart
    wait_login "$STAMP" && touch "$GW_DIR/.settings-initialised" && echo "Login completed after restart."
  fi
else
  echo "No 'Login has completed' yet. Check the log (a 2FA prompt, a wrong paper username/password, or a slow first start):  cd $GW_DIR && $DOCKER compose logs --tail 80" >&2
fi
echo "API port check:  ss -ltn | grep 4002   (127.0.0.1:4002 = paper)"
echo "Then wire qmag to it:  bash $APP_DIR/deploy/install-ibgateway.sh --wire"
