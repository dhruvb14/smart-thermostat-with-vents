#!/usr/bin/env bash
set -e

CONFIG_PATH=/data/options.json

export HA_URL="$(jq -r '.ha_url // empty' "$CONFIG_PATH" 2>/dev/null || echo "${HA_URL:-}")"
export HA_TOKEN="$(jq -r '.ha_token // empty' "$CONFIG_PATH" 2>/dev/null || echo "${HA_TOKEN:-}")"
export DATA_DIR="${DATA_DIR:-/data}"
export PORT="${PORT:-8099}"

# If running inside HA supervisor, use the supervisor proxy
if [ -n "${SUPERVISOR_TOKEN:-}" ]; then
    export HA_URL="${HA_URL:-http://supervisor/core}"
    export HA_TOKEN="${HA_TOKEN:-$SUPERVISOR_TOKEN}"
fi

mkdir -p "$DATA_DIR"

exec python3 -m backend.main
