#!/bin/sh
# shellcheck shell=sh
set -e

# ---------------------------------------------------------------------------
# Helper: read a key from /data/options.json (written by HA Supervisor).
# Falls back to $2 if the key is missing or null.
# ---------------------------------------------------------------------------
options_get() {
    _key="$1"
    _default="${2:-}"
    _val=$(jq -r --arg k "$_key" '.[$k] // empty' /data/options.json 2>/dev/null || true)
    printf '%s' "${_val:-$_default}"
}

log_info()    { echo "[INFO]    $*"; }
log_warning() { echo "[WARNING] $*"; }

# ---------------------------------------------------------------------------
# Diagnose token availability — logged before anything else so we can see
# which injection mechanism worked.
# ---------------------------------------------------------------------------
if [ -n "${SUPERVISOR_TOKEN:-}" ]; then
    log_info "SUPERVISOR_TOKEN: present via container environment"
else
    log_warning "SUPERVISOR_TOKEN: NOT found — supervisor injection may have failed"
fi

# ---------------------------------------------------------------------------
# Read add-on options from /data/options.json via jq
# ---------------------------------------------------------------------------
HA_URL_CFG=$(options_get 'ha_url' '')
HA_TOKEN_CFG=$(options_get 'ha_token' '')
USE_WSS=$(options_get 'use_wss' 'false')
SSL_VERIFY=$(options_get 'ssl_verify' 'true')
TIMEZONE=$(options_get 'timezone' 'UTC')
TEMPERATURE_UNIT=$(options_get 'temperature_unit' '')

# ---------------------------------------------------------------------------
# Resolve HA_URL and HA_TOKEN
# ---------------------------------------------------------------------------
if [ -n "${SUPERVISOR_TOKEN:-}" ]; then
    # Supervisor token is available.
    # URL: use user-configured URL if set, otherwise supervisor internal proxy.
    if [ -n "${HA_URL_CFG}" ]; then
        export HA_URL="${HA_URL_CFG}"
        log_info "URL source: user-config (${HA_URL})"
    else
        export HA_URL="http://supervisor/core"
        log_info "URL source: supervisor-proxy (http://supervisor/core)"
    fi

    # Token: use user-configured token if set, otherwise supervisor token.
    # This also allows combining a custom URL with the supervisor token —
    # just set ha_url but leave ha_token blank.
    if [ -n "${HA_TOKEN_CFG}" ]; then
        export HA_TOKEN="${HA_TOKEN_CFG}"
        log_info "Token source: user-config"
    else
        export HA_TOKEN="${SUPERVISOR_TOKEN}"
        log_info "Token source: SUPERVISOR_TOKEN"
    fi
else
    # No supervisor token — fall back entirely to user-configured credentials.
    export HA_URL="${HA_URL_CFG:-http://homeassistant.local:8123}"
    export HA_TOKEN="${HA_TOKEN_CFG:-}"
    log_warning "No supervisor token — relying on user-configured credentials"
fi

export HA_USE_WSS="${USE_WSS}"
export HA_SSL_VERIFY="${SSL_VERIFY}"
export TZ="${TIMEZONE:-UTC}"
export DATA_DIR="${DATA_DIR:-/config}"
export PORT="${PORT:-8099}"
export TEMPERATURE_UNIT="${TEMPERATURE_UNIT}"

log_info "HA_URL=${HA_URL} USE_WSS=${HA_USE_WSS} SSL_VERIFY=${HA_SSL_VERIFY} TZ=${TZ} TEMPERATURE_UNIT=${TEMPERATURE_UNIT:-auto}"

mkdir -p "${DATA_DIR}"

# ---------------------------------------------------------------------------
# One-time migration: copy database from legacy /data to /config so it
# becomes accessible via the Samba addon_configs share.  Only runs when the
# new location has no database yet.  options.json stays in /data — that is
# written by the Supervisor and is not ours to move.
# ---------------------------------------------------------------------------
if [ ! -f "${DATA_DIR}/app.db" ] && [ ! -f "${DATA_DIR}/flair.db" ]; then
    for _src in flair.db app.db; do
        if [ -f "/data/${_src}" ]; then
            log_info "Migrating ${_src} from /data to ${DATA_DIR}"
            cp "/data/${_src}" "${DATA_DIR}/${_src}"
            [ -f "/data/${_src}-wal" ] && cp "/data/${_src}-wal" "${DATA_DIR}/${_src}-wal"
            [ -f "/data/${_src}-shm" ] && cp "/data/${_src}-shm" "${DATA_DIR}/${_src}-shm"
            break
        fi
    done
fi

exec python3 -m backend.main
