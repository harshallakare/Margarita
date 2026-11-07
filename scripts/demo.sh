#!/usr/bin/env bash
# Purpose: run a self-contained local demo of the platform (API + conductor + worker + web UI) on
#          fake providers. State lives in ./vmigrate-demo (delete it to start over).
# Usage:   scripts/demo.sh [port]      -> http://127.0.0.1:<port>/ui/   (login: admin / demo-admin-password)
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${1:-8080}"
DATA="$PWD/vmigrate-demo"
mkdir -p "$DATA"
[ -s "$DATA/secret.key" ] || .venv/bin/vmigrate-platform gen-key > "$DATA/secret.key"
chmod 600 "$DATA/secret.key"
export VMIGRATE_SECRET_KEY_FILE="$DATA/secret.key"
export VMIGRATE_DATABASE_URL="sqlite:///$DATA/platform.db"
export VMIGRATE_DATA_DIR="$DATA"
export VMIGRATE_POLL_SECONDS="${VMIGRATE_POLL_SECONDS:-1}"
export VMIGRATE_WEB_DIR="$PWD/web/dist"
[ -f web/dist/index.html ] || (cd web && npm install && npm run build)
.venv/bin/python scripts/demo_seed.py
exec .venv/bin/vmigrate-platform all --port "$PORT"
