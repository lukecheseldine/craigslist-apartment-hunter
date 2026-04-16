#!/usr/bin/env bash
# Run from your machine (not the server). Requires SSH access to the droplet.
set -euo pipefail

HOST="${DEPLOY_HOST:-root@64.23.228.246}"
REMOTE_DIR="${DEPLOY_REMOTE_DIR:-/root/craigslist-apartment-hunter}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Deploying from $ROOT to $HOST:$REMOTE_DIR"
scp "$ROOT/craigslist_watch.py" "$ROOT/README.md" "$ROOT/requirements.txt" "$HOST:$REMOTE_DIR/"

ssh "$HOST" "set -e
cd '$REMOTE_DIR'
./.venv/bin/pip install -q -r requirements.txt
./.venv/bin/python craigslist_watch.py
echo 'Smoke run exit code:' \$?
"

echo "Done."
