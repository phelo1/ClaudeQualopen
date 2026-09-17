#!/usr/bin/env sh
# Runs the 24/7 daemon and the dashboard in one container.
# Live brokers need an explicit opt-in (QMAG_YES_LIVE=1) because there is no
# TTY to confirm on.
set -eu

BROKER="${QMAG_BROKER:-paper}"
STATE="${QMAG_STATE_DIR:-/app/paper_state}"
PORT="${QMAG_DASHBOARD_PORT:-8765}"
EXTRA="${QMAG_ARGS:-}"

LIVE_FLAG=""
case "$BROKER" in
  *-live)
    if [ "${QMAG_YES_LIVE:-0}" != "1" ]; then
      echo "Refusing to start a LIVE broker without QMAG_YES_LIVE=1" >&2
      exit 2
    fi
    LIVE_FLAG="--yes-live"
    ;;
esac

if [ ! -f universe/market.txt ]; then
  echo "No universe/market.txt yet; building the whole-market universe first..."
  qmag universe build || echo "Universe build failed; falling back to universe/default.txt"
fi

qmag dashboard --host 0.0.0.0 --port "$PORT" --broker "$BROKER" --state-dir "$STATE" $EXTRA &
DASH=$!
qmag daemon --broker "$BROKER" --state-dir "$STATE" $LIVE_FLAG $EXTRA &
DAEMON=$!
cleanup() {
  kill "$DASH" "$DAEMON" 2>/dev/null || true
  wait "$DASH" 2>/dev/null || true
  wait "$DAEMON" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
while kill -0 "$DASH" 2>/dev/null && kill -0 "$DAEMON" 2>/dev/null; do
  sleep 2
done
echo 'A supervised service exited; stopping its sibling.' >&2
exit 1
