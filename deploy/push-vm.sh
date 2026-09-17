#!/usr/bin/env bash
# Ship the committed tree to a VM over SSH and (re)install it there.
#
#   deploy/push-vm.sh ubuntu@1.2.3.4 [-i ~/.ssh/key] [--port 8855] [--broker paper|ibkr|...] [--dir ~/qmag]
#   --broker is only needed to CHANGE the broker; without it the installed unit keeps its current one.
#
# Only what `git archive HEAD` contains is sent: no .venv, no state, no secrets.
# Existing paper_state/, data/ and .env on the VM are left untouched.
set -euo pipefail

TARGET="${1:?usage: deploy/push-vm.sh user@host [-i key] [--port N] [--broker B] [--dir PATH]}"
shift
KEY=""; PORT=8855; BROKER=""; APP_DIR='$HOME/qmag'
while [ $# -gt 0 ]; do
  case "$1" in
    -i) KEY="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --broker) BROKER="$2"; shift 2 ;;
    --dir) APP_DIR="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 1 ;;
  esac
done

SSH=(ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
[ -n "$KEY" ] && SSH+=(-i "$KEY")

cd "$(git rev-parse --show-toplevel)"
REV="$(git rev-parse --short HEAD)"
echo "Pushing $REV to $TARGET:$APP_DIR"

git archive --format=tar HEAD | "${SSH[@]}" "$TARGET" "mkdir -p $APP_DIR && tar -x -C $APP_DIR && echo $REV > $APP_DIR/.deployed-rev"
"${SSH[@]}" "$TARGET" "APP_DIR=$APP_DIR PORT=$PORT BROKER=$BROKER bash $APP_DIR/deploy/install-vm.sh"  # empty BROKER = keep the installed one
