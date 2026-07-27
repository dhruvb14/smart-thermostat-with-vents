#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

# Helper function: try bashio first, fall back to uppercase env var
get_config() {
    local key=$1
    local default=$2

    # Check if bashio is available and the key exists in options.json
    if command -v bashio >/dev/null 2>&1 && bashio::config.has_value "$key" 2>/dev/null; then
        bashio::config "$key"
        return
    fi

    # Fallback: use uppercase environment variable (e.g. 'timezone' → 'TIMEZONE')
    local env_var=$(echo "$key" | tr '[:lower:]' '[:upper:]')
    echo "${!env_var:-$default}"
}

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
# Read add-on options from /data/options.json (HAOS) or environment
# variables (Docker). The get_config helper checks bashio.has_value first
# to avoid error spam when supervisor is unavailable.
# ---------------------------------------------------------------------------
HA_URL_CFG=$(get_config 'ha_url' '')
HA_TOKEN_CFG=$(get_config 'ha_token' '')
USE_WSS=$(get_config 'use_wss' 'false')
SSL_VERIFY=$(get_config 'ssl_verify' 'true')
TIMEZONE=$(get_config 'timezone' 'UTC')
# Default to EMPTY (not 'F') so a blank add-on option means "auto-detect from
# Home Assistant". An empty TEMPERATURE_UNIT lets the backend resolve the unit
# from HA's /api/config and the last-known DB value; exporting 'F' here would be
# treated as a hard override lock in the scheduler, defeating auto-detect. (#281)
TEMPERATURE_UNIT=$(get_config 'temperature_unit' '')
# Require auth on directly-exposed ports + MCP (#373). Default 'true' (secure);
# HA ingress is always trusted regardless. Empty add-on option → 'true'.
REQUIRE_AUTH=$(get_config 'require_auth' 'true')
# OIDC single sign-on for the web UI (#464). All OPTIONAL — blank means the
# default HA username/password login is used on direct ports. Each add-on option
# key uppercases to the exact env var the backend reads, and get_config falls
# back to that env var when there is no Supervisor, so standalone-Docker operators
# set OIDC_* / PLENUM_EXTERNAL_URL directly. MCP is unaffected.
OIDC_CONFIGURATION_URL=$(get_config 'oidc_configuration_url' '')
OIDC_CLIENT_ID=$(get_config 'oidc_client_id' '')
OIDC_CLIENT_SECRET=$(get_config 'oidc_client_secret' '')
OIDC_SCOPES=$(get_config 'oidc_scopes' 'openid email profile')
OIDC_ALLOWED_USERS_GLOB=$(get_config 'oidc_allowed_users_glob' '*')
OIDC_PROVIDER_NAME=$(get_config 'oidc_provider_name' '')
PLENUM_EXTERNAL_URL=$(get_config 'plenum_external_url' '')
# MQTT interface for HA automations (#519). All OPTIONAL. Blank broker fields
# fall back to Supervisor MQTT service discovery, and the topic prefix defaults
# to the add-on slug — both resolved by the backend over the Supervisor REST API
# (see backend/mqtt/config.py). Whether the bridge actually connects is the
# runtime toggle on Plenum's Settings page; nothing here switches it on.
MQTT_HOST=$(get_config 'mqtt_host' '')
MQTT_PORT=$(get_config 'mqtt_port' '1883')
MQTT_USER=$(get_config 'mqtt_user' '')
MQTT_PASSWORD=$(get_config 'mqtt_password' '')
MQTT_DISCOVERY=$(get_config 'mqtt_discovery' 'true')
MQTT_DISCOVERY_PREFIX=$(get_config 'mqtt_discovery_prefix' 'homeassistant')
MQTT_TOPIC_PREFIX=$(get_config 'mqtt_topic_prefix' '')


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
export MCP_PORT="${MCP_PORT:-9099}"
export TEMPERATURE_UNIT="${TEMPERATURE_UNIT}"
export REQUIRE_AUTH="${REQUIRE_AUTH}"
export OIDC_CONFIGURATION_URL="${OIDC_CONFIGURATION_URL}"
export OIDC_CLIENT_ID="${OIDC_CLIENT_ID}"
export OIDC_CLIENT_SECRET="${OIDC_CLIENT_SECRET}"
export OIDC_SCOPES="${OIDC_SCOPES}"
export OIDC_ALLOWED_USERS_GLOB="${OIDC_ALLOWED_USERS_GLOB}"
export OIDC_PROVIDER_NAME="${OIDC_PROVIDER_NAME}"
export PLENUM_EXTERNAL_URL="${PLENUM_EXTERNAL_URL}"
export MQTT_HOST="${MQTT_HOST}"
export MQTT_PORT="${MQTT_PORT}"
export MQTT_USER="${MQTT_USER}"
export MQTT_PASSWORD="${MQTT_PASSWORD}"
export MQTT_DISCOVERY="${MQTT_DISCOVERY}"
export MQTT_DISCOVERY_PREFIX="${MQTT_DISCOVERY_PREFIX}"
export MQTT_TOPIC_PREFIX="${MQTT_TOPIC_PREFIX}"

bashio::log.info "HA_URL=${HA_URL} USE_WSS=${HA_USE_WSS} SSL_VERIFY=${HA_SSL_VERIFY} TZ=${TZ} TEMPERATURE_UNIT=${TEMPERATURE_UNIT:-auto} REQUIRE_AUTH=${REQUIRE_AUTH}"
# Log OIDC status WITHOUT the client secret (never log secrets).
if [ -n "${OIDC_CONFIGURATION_URL}" ]; then
    bashio::log.info "OIDC: configured (provider='${OIDC_PROVIDER_NAME:-SSO}', external_url='${PLENUM_EXTERNAL_URL:-<unset>}', allowlist='${OIDC_ALLOWED_USERS_GLOB}')"
else
    bashio::log.info "OIDC: not configured (using HA username/password login on direct ports)"
fi

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
