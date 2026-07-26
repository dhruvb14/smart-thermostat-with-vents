"""Resolving the MQTT broker connection and instance topic prefix (Issue #519).

Two things are resolved here, both once at boot, and both **unconditionally** —
there is no deployment-level enable switch. The first HAOS field test showed why:
a `mqtt_enabled` add-on option meant a user with the Mosquitto add-on already
running still saw "not configured" until they found the option, which is exactly
the zero-config experience #519 promised to avoid. The runtime toggle on the
Settings page (`system_settings.mqtt_enabled`, the twin of the MCP server's) is
the one and only switch; resolving the broker is free and connects nothing.

**Broker connection.** Under the HA Supervisor the broker is discovered from the
built-in MQTT service (``services: - mqtt:want`` in ``config.yaml``), so a HAOS
user configures nothing. Standalone Docker has no Supervisor, so the
``MQTT_HOST`` / ``MQTT_PORT`` / ``MQTT_USER`` / ``MQTT_PASSWORD`` variables are
the fallback. An explicitly configured host always wins over discovery — an
operator who names a broker means it.

**Topic prefix.** Stable and beta are separate add-ons sharing one broker, so
their topic trees must not collide. The Supervisor knows each install's unique
slug (``plenum`` vs ``plenum_beta``), fetched here from
``/addons/self/info`` — the same Supervisor REST API the broker discovery uses.
It is deliberately NOT fetched via ``bashio`` in ``run.sh``: the first HAOS
field test showed that approach silently yielding an empty slug (the beta
add-on booted with the stable ``plenum`` prefix), while the Python REST calls
in this module worked in the same boot. Standalone Docker has no slug at all,
so it falls back to a hardcoded default and warns rather than refusing to
start. ``mqtt_topic_prefix`` overrides both in either mode.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from .naming import sanitize

log = logging.getLogger(__name__)

# Used when there is no Supervisor slug and no override — i.e. standalone
# Docker. Two such containers on one broker collide; we warn and continue.
DEFAULT_PREFIX = "plenum"

# Where HA looks for MQTT Discovery config topics. Configurable because the
# `discovery_prefix` of the HA MQTT integration is itself user-settable.
DEFAULT_DISCOVERY_PREFIX = "homeassistant"

DEFAULT_PORT = 1883


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class MqttConfig:
    """Everything the bridge needs to connect and name its topics."""

    host: str
    port: int
    username: str | None
    password: str | None
    prefix: str
    discovery: bool
    discovery_prefix: str
    # True when `prefix` came from the hardcoded default rather than a slug or
    # an explicit override — the one case where two installs can collide.
    prefix_is_fallback: bool = False

    @property
    def configured(self) -> bool:
        """Whether there is a broker to connect to at all.

        This is the availability gate: with no resolvable broker the bridge
        never starts. Whether an *available* bridge actually connects is the
        user's runtime toggle, checked live by the bridge loop.
        """
        return bool(self.host)


def _supervisor_get(path: str) -> dict | None:
    """Fetch one Supervisor REST endpoint; ``None`` on any failure.

    Never fatal — no Supervisor, a missing service, or a denied request all
    just mean "fall back". Uses a blocking urllib call because this runs
    exactly once during startup wiring, before the bridge's event loop work
    begins, and pulling in an async client for one boot-time request is not
    worth the complexity.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"http://supervisor{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - fixed internal URL
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        log.info("Supervisor endpoint %s not available", path)
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def _supervisor_mqtt() -> dict | None:
    """Broker details from the Supervisor's built-in MQTT service, or ``None``."""
    return _supervisor_get("/services/mqtt")


def _supervisor_slug() -> str:
    """This add-on's own slug from the Supervisor, or ``""``.

    ``/addons/self/info`` is what distinguishes the stable install (``plenum``)
    from the beta (``plenum_beta``) with zero configuration. Resolved here in
    Python — not via ``bashio`` in ``run.sh`` — because the shell route was
    observed returning nothing on a real HAOS install while this API worked.
    """
    data = _supervisor_get("/addons/self/info")
    if not data:
        return ""
    return str(data.get("slug") or "")


def resolve_prefix(override: str, slug: str) -> tuple[str, bool]:
    """Resolve the instance topic prefix. Returns ``(prefix, is_fallback)``.

    Precedence: explicit override → add-on slug → :data:`DEFAULT_PREFIX`. Every
    candidate is sanitised, and a candidate that sanitises away to nothing (say
    ``"!!!"``) is skipped rather than silently yielding an empty segment.
    """
    for candidate in (override, slug):
        cleaned = sanitize(candidate)
        if cleaned:
            return cleaned, False
    return DEFAULT_PREFIX, True


def load_config(
    supervisor_lookup=_supervisor_mqtt,
    slug_lookup=_supervisor_slug,
) -> MqttConfig:
    """Build the :class:`MqttConfig` from the environment.

    The two lookups are injected so tests can drive the Supervisor-present and
    standalone-Docker paths without a Supervisor. Both are skipped when the
    corresponding value is already pinned (an explicit ``MQTT_HOST``; an
    ``MQTT_TOPIC_PREFIX`` override or ``ADDON_SLUG``) — never second-guess an
    operator, and never make a network call whose answer would be ignored.
    """
    host = _env("MQTT_HOST")
    port_raw = _env("MQTT_PORT")
    username = _env("MQTT_USER") or None
    password = _env("MQTT_PASSWORD") or None

    # Only reach for the Supervisor when the operator has not named a broker.
    if not host:
        discovered = supervisor_lookup()
        if discovered:
            host = str(discovered.get("host") or "")
            port_raw = port_raw or str(discovered.get("port") or "")
            username = username or (discovered.get("username") or None)
            password = password or (discovered.get("password") or None)
            if host:
                log.info("MQTT broker discovered via Supervisor at %s:%s", host, port_raw or "-")

    try:
        port = int(port_raw) if port_raw else DEFAULT_PORT
    except ValueError:
        log.warning("Invalid MQTT_PORT %r — falling back to %d", port_raw, DEFAULT_PORT)
        port = DEFAULT_PORT

    override = _env("MQTT_TOPIC_PREFIX")
    slug = _env("ADDON_SLUG")
    # The slug only matters when nothing else pins the prefix, and it only
    # exists where a broker can be discovered — don't ask the Supervisor for it
    # when there is no broker to name topics on.
    if not sanitize(override) and not sanitize(slug) and host:
        slug = slug_lookup()
        if slug:
            log.info("Add-on slug resolved via Supervisor: %r", slug)
    prefix, is_fallback = resolve_prefix(override, slug)

    return MqttConfig(
        host=host,
        port=port,
        username=username,
        password=password,
        prefix=prefix,
        discovery=_env_bool("MQTT_DISCOVERY", True),
        discovery_prefix=sanitize(_env("MQTT_DISCOVERY_PREFIX")) or DEFAULT_DISCOVERY_PREFIX,
        prefix_is_fallback=is_fallback,
    )


def log_resolution(config: MqttConfig) -> None:
    """Announce the resolved broker + prefix at startup, never silently."""
    if not config.configured:
        log.info(
            "MQTT: no broker resolved (no Supervisor MQTT service and no MQTT_HOST) — "
            "the bridge is unavailable until one exists"
        )
        return
    log.info(
        "MQTT broker %s:%d, topic prefix %r (discovery=%s) — the bridge connects "
        "once the Settings-page toggle is on",
        config.host,
        config.port,
        config.prefix,
        config.discovery,
    )
    if config.prefix_is_fallback:
        if os.environ.get("SUPERVISOR_TOKEN"):
            # A Supervisor is present, so a slug SHOULD have resolved; falling
            # back here means the /addons/self/info lookup failed. Stable and
            # beta on one broker would collide on the shared default.
            log.warning(
                "MQTT topic prefix fell back to the default %r — the Supervisor is "
                "present but the add-on slug could not be resolved. If two Plenum "
                "add-ons share this broker, set mqtt_topic_prefix on at least one.",
                config.prefix,
            )
        else:
            log.warning(
                "MQTT topic prefix fell back to the default %r — no add-on slug exists "
                "without a Supervisor (standalone Docker). Two Plenum containers on the "
                "same broker WILL collide; set MQTT_TOPIC_PREFIX on at least one of them.",
                config.prefix,
            )
