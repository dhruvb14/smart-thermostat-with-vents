#!/usr/bin/env bash
# docker-entrypoint.sh — Docker-mode shim
#
# with-contenv (used by run.sh's shebang) replaces the process environment
# with only what is in /var/run/s6/container_environment/.  In HAOS the
# Supervisor populates that directory; in plain Docker it is empty, so every
# variable passed via "docker run -e" would be discarded.
#
# This shim runs BEFORE with-contenv: it writes the known config variables
# into /var/run/s6/container_environment/ so they survive the transition.

set -e

mkdir -p /var/run/s6/container_environment

for _var in TIMEZONE TEMPERATURE_UNIT HA_URL HA_TOKEN USE_WSS SSL_VERIFY DATA_DIR PORT; do
    eval "_val=\${${_var}:-}"
    if [ -n "$_val" ]; then
        printf '%s' "$_val" > "/var/run/s6/container_environment/${_var}"
    fi
done

exec /run.sh "$@"
