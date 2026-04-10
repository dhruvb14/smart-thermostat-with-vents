#!/command/with-contenv bashio
# shellcheck shell=bash
set -e

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
# Read add-on options from /data/options.json via bashio
# ---------------------------------------------------------------------------
HA_URL_CFG=$(bashio::config 'ha_url' 2>/dev/null || echo "")
HA_TOKEN_CFG=$(bashio::config 'ha_token' 2>/dev/null || echo "")
USE_WSS=$(bashio::config 'use_wss' 2>/dev/null || echo "false")
SSL_VERIFY=$(bashio::config 'ssl_verify' 2>/dev/null || echo "true")
TIMEZONE=$(bashio::config 'timezone' 2>/dev/null || echo "UTC")

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
export DATA_DIR="${DATA_DIR:-/data}"
export PORT="${PORT:-8099}"

bashio::log.info "HA_URL=${HA_URL} USE_WSS=${HA_USE_WSS} SSL_VERIFY=${HA_SSL_VERIFY} TZ=${TZ}"

mkdir -p "${DATA_DIR}"

exec python3 -m backend.main
