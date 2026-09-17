#!/usr/bin/env bash
# Record the quick tunnel's current public URL and tell you when it changes.
#
# A quick tunnel (no Cloudflare account) gets a new random
# https://<words>.trycloudflare.com address every time cloudflared restarts -
# after a reboot, an update or a dropped connection. This script, run every
# minute by the qmag-tunnel-watch timer that deploy/install-tunnel.sh installs,
# reads the current address from the tunnel's log, writes it to
# <state-dir>/tunnel_url.txt (shown on the dashboard's settings page) and,
# when it differs from the last one recorded, pushes an alert over the desk's
# configured channels (Telegram / webhook) so the new address reaches your
# phone without you having to log in to the box.
#
#   APP_DIR=~/qmag STATE_DIR=~/qmag/paper_state bash deploy/tunnel-watch.sh
set -uo pipefail

APP_DIR="${APP_DIR:-$HOME/qmag}"
STATE_DIR="${STATE_DIR:-$APP_DIR/paper_state}"
FILE="$STATE_DIR/tunnel_url.txt"

if ! systemctl is-active -q qmag-tunnel; then
  exit 0
fi
URL="$(journalctl -u qmag-tunnel --no-pager -o cat 2>/dev/null | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1 || true)"
[ -z "$URL" ] && exit 0

OLD=""
[ -f "$FILE" ] && OLD="$(cat "$FILE" 2>/dev/null || true)"
if [ "$URL" = "$OLD" ]; then
  exit 0
fi

mkdir -p "$STATE_DIR"
printf '%s\n' "$URL" > "$FILE"
if [ -n "$OLD" ]; then
  TITLE="Dashboard address changed"
else
  TITLE="Dashboard address"
fi
if [ -x "$APP_DIR/.venv/bin/qmag" ]; then
  # Exit 2 = no alert channel configured; the file is still written.
  "$APP_DIR/.venv/bin/qmag" alert --state-dir "$STATE_DIR" "$TITLE" "$URL (the tunnel restarted; the old address no longer works)" >/dev/null 2>&1 || true
fi
echo "$URL"
