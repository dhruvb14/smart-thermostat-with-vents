#!/command/with-contenv bashio
# shellcheck shell=bash

####!/usr/bin/with-contenv bash
#### shellcheck shell=bash
set -e

# Mock bashio if not present (for CI/local testing)
# if ! command -v bashio::log.info >/dev/null 2>&1; then
#     bashio::log.info() { echo "[$(date +'%H:%M:%S')] INFO: $*"; }
#     bashio::log.warning() { echo "[$(date +'%H:%M:%S')] WARNING: $*"; }
#     bashio::log.error() { echo "[$(date +'%H:%M:%S')] ERROR: $*"; }
#     bashio::config() { return 1; }
# fi

# ---------------------------------------------------------------------------
# Diagnose token availability — logged before anything else so we can see
# which injection mechanism worked.
# ---------------------------------------------------------------------------
if [ -n "${SUPERVISOR_TOKEN:-}" ]; then
    bashio::log.info "SUPERVISOR_TOKEN: present via container environment (with-contenv)"
else
    bashio::log.warning "SUPERVISOR_TOKEN: NOT found — supervisor injection may have failed"
fi

# ---------------------------------------------------------------------------
# Read add-on options from /data/options.json via bashio, or fall back to
# environment variables (for Docker mode where bashio is not available)
# ---------------------------------------------------------------------------
if HA_URL_CFG=$(bashio::config 'ha_url' 2>/dev/null); then : ; else HA_URL_CFG="${HA_URL:-}"; fi
if HA_TOKEN_CFG=$(bashio::config 'ha_token' 2>/dev/null); then : ; else HA_TOKEN_CFG="${HA_TOKEN:-}"; fi
if USE_WSS=$(bashio::config 'use_wss' 2>/dev/null); then : ; else USE_WSS="${USE_WSS:-false}"; fi
if SSL_VERIFY=$(bashio::config 'ssl_verify' 2>/dev/null); then : ; else SSL_VERIFY="${SSL_VERIFY:-true}"; fi
if TIMEZONE=$(bashio::config 'timezone' 2>/dev/null); then : ; else TIMEZONE="${TIMEZONE:-UTC}"; fi
if TEMPERATURE_UNIT=$(bashio::config 'temperature_unit' 2>/dev/null); then : ; else TEMPERATURE_UNIT="${TEMPERATURE_UNIT:-F}"; fi


# ---------------------------------------------------------------------------
# Resolve HA_URL and HA_TOKEN
# ---------------------------------------------------------------------------
if [ -n "${SUPERVISOR_TOKEN:-}" ]; then
    # Supervisor token is available.
    # URL: use user-configured URL if set, otherwise supervisor internal proxy.
    if [ -n "${HA_URL_CFG}" ]; then
        export HA_URL="${HA_URL_CFG}"
        bashio::log.info "URL source: user-config (${HA_URL})"
    else
        export HA_URL="http://supervisor/core"
        bashio::log.info "URL source: supervisor-proxy (http://supervisor/core)"
    fi

    # Token: use user-configured token if set, otherwise supervisor token.
    # This also allows combining a custom URL with the supervisor token —
    # just set ha_url but leave ha_token blank.
    if [ -n "${HA_TOKEN_CFG}" ]; then
        export HA_TOKEN="${HA_TOKEN_CFG}"
        bashio::log.info "Token source: user-config"
    else
        export HA_TOKEN="${SUPERVISOR_TOKEN}"
        bashio::log.info "Token source: SUPERVISOR_TOKEN"
    fi
else
    # No supervisor token — fall back entirely to user-configured credentials.
    export HA_URL="${HA_URL_CFG:-http://homeassistant.local:8123}"
    export HA_TOKEN="${HA_TOKEN_CFG:-}"
    bashio::log.warning "No supervisor token — relying on user-configured credentials"
fi

export HA_USE_WSS="${USE_WSS}"
export HA_SSL_VERIFY="${SSL_VERIFY}"
export TZ="${TIMEZONE:-UTC}"
export DATA_DIR="${DATA_DIR:-/config}"
export PORT="${PORT:-8099}"
export TEMPERATURE_UNIT="${TEMPERATURE_UNIT}"

bashio::log.info "HA_URL=${HA_URL} USE_WSS=${HA_USE_WSS} SSL_VERIFY=${HA_SSL_VERIFY} TZ=${TZ} TEMPERATURE_UNIT=${TEMPERATURE_UNIT:-auto}"

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
            bashio::log.info "Migrating ${_src} from /data to ${DATA_DIR}"
            cp "/data/${_src}" "${DATA_DIR}/${_src}"
            [ -f "/data/${_src}-wal" ] && cp "/data/${_src}-wal" "${DATA_DIR}/${_src}-wal"
            [ -f "/data/${_src}-shm" ] && cp "/data/${_src}-shm" "${DATA_DIR}/${_src}-shm"
            break
        fi
    done
fi

# ---------------------------------------------------------------------------
# CI Smoke Test: if the CI env var is set, exit after 10 seconds.
# This gives the backend enough time to attempt startup and fail if deps are missing.
# ---------------------------------------------------------------------------
if [ "${CI:-}" = "true" ]; then
    bashio::log.info "CI Smoke Test enabled — will exit in 10s..."
    (sleep 10 && bashio::log.info "CI Smoke Test complete, shutting down." && kill 1) &
fi

exec python3 -m backend.main
