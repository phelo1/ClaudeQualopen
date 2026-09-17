#!/usr/bin/env bash
# Print the current public URL of the quick tunnel started by deploy/install-tunnel.sh.
set -euo pipefail
if ! systemctl is-active -q qmag-tunnel; then
  echo "qmag-tunnel is not running (sudo systemctl start qmag-tunnel)" >&2
  exit 1
fi
URL="$(journalctl -u qmag-tunnel --no-pager -o cat | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1 || true)"
if [ -z "$URL" ]; then
  echo "no trycloudflare hostname in the log (named tunnel, or still connecting)" >&2
  exit 1
fi
echo "$URL"
