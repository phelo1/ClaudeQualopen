#!/usr/bin/env bash
# Expose the dashboard to the internet over HTTPS with a Cloudflare Tunnel.
#
#   PORT=8855 bash deploy/install-tunnel.sh            # quick tunnel (no account needed)
#   PORT=8855 TUNNEL_TOKEN=eyJ... bash deploy/install-tunnel.sh   # named tunnel from the Cloudflare Zero Trust dashboard
#
# The tunnel is an outbound connection from this machine to Cloudflare: no
# inbound port is opened here and the dashboard stays bound to 127.0.0.1.
#
# Quick tunnels get a random https://<words>.trycloudflare.com hostname that
# changes whenever cloudflared restarts; `deploy/tunnel-url.sh` prints the
# current one.  A named tunnel (needs a free Cloudflare account and a domain on
# it) gives a fixed hostname and can sit behind Cloudflare Access as well.
#
# Set QMAG_DASHBOARD_PASSWORD before exposing anything: the installer refuses
# to start a tunnel to an unprotected dashboard.
set -euo pipefail

PORT="${PORT:-8855}"
APP_DIR="${APP_DIR:-$HOME/qmag}"
STATE_DIR="${STATE_DIR:-$APP_DIR/paper_state}"
TUNNEL_TOKEN="${TUNNEL_TOKEN:-}"

if ! grep -qs '^QMAG_DASHBOARD_PASSWORD=.\+' "$STATE_DIR/settings.env" "$APP_DIR/.env" 2>/dev/null && [ -z "${QMAG_DASHBOARD_PASSWORD:-}" ]; then
  echo "Refusing: no QMAG_DASHBOARD_PASSWORD in $STATE_DIR/settings.env or $APP_DIR/.env. Set one first (settings page → Remote access)." >&2
  exit 2
fi

if ! command -v cloudflared >/dev/null; then
  case "$(uname -m)" in
    aarch64|arm64) ARCH=arm64 ;;
    x86_64|amd64) ARCH=amd64 ;;
    *) echo "unsupported arch $(uname -m)" >&2; exit 1 ;;
  esac
  TMP="$(mktemp -d)"
  curl -fsSL -o "$TMP/cloudflared.deb" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH.deb"
  sudo dpkg -i "$TMP/cloudflared.deb" >/dev/null
  rm -rf "$TMP"
fi
cloudflared --version

if [ -n "$TUNNEL_TOKEN" ]; then
  EXEC="/usr/bin/cloudflared tunnel --no-autoupdate run --token $TUNNEL_TOKEN"
  KIND="named"
else
  EXEC="/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:$PORT"
  KIND="quick"
fi

sudo tee /etc/systemd/system/qmag-tunnel.service >/dev/null <<EOF
[Unit]
Description=qmag dashboard Cloudflare tunnel ($KIND)
After=network-online.target qmag-dashboard.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=$EXEC
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable -q qmag-tunnel
sudo systemctl restart qmag-tunnel

if [ "$KIND" = "quick" ]; then
  # Watcher: records the current address in $STATE_DIR/tunnel_url.txt and
  # alerts (Telegram / webhook) when a restart hands out a new one.
  WATCH="$(cd "$(dirname "$0")" && pwd)/tunnel-watch.sh"
  chmod +x "$WATCH"
  sudo tee /etc/systemd/system/qmag-tunnel-watch.service >/dev/null <<EOF
[Unit]
Description=qmag: record the quick tunnel's public URL and alert when it changes

[Service]
Type=oneshot
User=$USER
Environment=APP_DIR=$APP_DIR
Environment=STATE_DIR=$STATE_DIR
ExecStart=/usr/bin/env bash $WATCH
EOF
  sudo tee /etc/systemd/system/qmag-tunnel-watch.timer >/dev/null <<EOF
[Unit]
Description=qmag: check the tunnel address every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=15s

[Install]
WantedBy=timers.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable -q --now qmag-tunnel-watch.timer

  echo "Waiting for the tunnel hostname..."
  for _ in $(seq 1 30); do
    URL="$(journalctl -u qmag-tunnel --since '-2 min' --no-pager -o cat 2>/dev/null | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1 || true)"
    [ -n "$URL" ] && break
    sleep 2
  done
  echo "Dashboard: ${URL:-<not up yet; run deploy/tunnel-url.sh>}"
else
  echo "Named tunnel running; the hostname is whatever you mapped to http://127.0.0.1:$PORT in Zero Trust → Networks → Tunnels."
fi
