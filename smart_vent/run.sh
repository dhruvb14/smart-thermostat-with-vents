#!/usr/bin/env bash
set -e

CONFIG_PATH=/data/options.json

# Read user-configured values from options.json into local vars (may be empty)
HA_URL_CFG="$(jq -r '.ha_url // empty' "$CONFIG_PATH" 2>/dev/null || true)"
HA_TOKEN_CFG="$(jq -r '.ha_token // empty' "$CONFIG_PATH" 2>/dev/null || true)"
USE_WSS_CFG="$(jq -r '.use_wss // "false"' "$CONFIG_PATH" 2>/dev/null || echo "false")"

# When running under HA Supervisor, prefer the supervisor proxy unless the
# user has explicitly set an override URL in the add-on options.
if [ -n "${SUPERVISOR_TOKEN:-}" ]; then
    export HA_URL="${HA_URL_CFG:-http://supervisor/core}"
    export HA_TOKEN="${HA_TOKEN_CFG:-$SUPERVISOR_TOKEN}"
else
    # Standalone Docker: fall back to env vars or defaults
    export HA_URL="${HA_URL_CFG:-${HA_URL:-http://homeassistant.local:8123}}"
    export HA_TOKEN="${HA_TOKEN_CFG:-${HA_TOKEN:-}}"
fi

export HA_USE_WSS="${USE_WSS_CFG}"
export DATA_DIR="${DATA_DIR:-/data}"
export PORT="${PORT:-8099}"

echo "Starting: HA_URL=${HA_URL} USE_WSS=${HA_USE_WSS} DATA_DIR=${DATA_DIR} PORT=${PORT}"

mkdir -p "$DATA_DIR"

exec python3 -m backend.main
