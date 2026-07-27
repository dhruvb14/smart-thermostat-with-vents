"""Home Assistant MQTT Discovery payloads (Issue #519).

Discovery config topics live under **HA's** discovery prefix (default
``homeassistant/``), not ours::

    <discovery_prefix>/<component>/<node_id>/<object_id>/config

Our instance prefix is folded into the ``node_id``/``unique_id`` inside the
payload, while the payload's ``state_topic``/``command_topic`` point back at our
own tree.

**Discovery is always id-based.** ``unique_id`` and the device ``identifiers``
derive from the room GUID or the thermostat ``entity_id``, never from a room's
name — so renaming a room never disturbs HA's entity registry, never orphans
history, and never produces a second entity for the same control. The
name-addressed topics are a raw-MQTT convenience for hand-written automations;
HA's entity list never sees them.

A config topic published with an empty payload removes the entity, which is how
deleted rooms, thermostats, and schedules are retired.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .naming import sanitize
from .registry import (
    KIND_BOOL,
    KIND_DATETIME,
    KIND_ENUM,
    KIND_NUMBER,
    TEMP_ABSOLUTE,
    Control,
)
from .topics import VERB_CLEAR, VERB_SET, command_topic, state_topic


def instance_title(prefix: str) -> str:
    """Human form of the instance's topic prefix, for device metadata.

    The prefix is the instance's identity (add-on slug on HAOS, or
    ``MQTT_TOPIC_PREFIX``), so it is what distinguishes two Plenum installs
    sharing a broker: ``plenum`` → "Plenum", ``plenum_beta`` → "Plenum Beta".
    It leads every device name, names the hub ("<instance> App"), and fills
    ``manufacturer`` — and because HA derives entity ids from the device name,
    it is also what makes a beta install's entities ``*.plenum_beta_…``.

    A HAOS add-on installed from a repository gets a slug of
    ``<8-hex-repo-hash>_<name>`` (``88b5ffac_plenum_beta``) and a locally built
    one gets ``local_<name>``. Neither token means anything to a human, so both
    are dropped from the title — they stay in the topics and identifiers, which
    keep the full prefix.
    """
    words = [w for w in prefix.strip("/").replace("-", "_").split("_") if w]
    if len(words) > 1 and (words[0] == "local" or re.fullmatch(r"[0-9a-f]{8}", words[0])):
        words = words[1:]
    return " ".join(w.capitalize() for w in words) or "Plenum"


# HA number entities enforce min/max on both input and *state*, so an advertised
# bound wider than the REST validator lets HA offer values that snap back, and a
# narrower one makes HA reject a legitimate state as out of range. In °C mode
# the registry's °F bounds must therefore be converted — and rounded *inward*
# (min up, max down) so both endpoints convert back inside the REST range. Step
# is coarsened to 0.1 °C rather than converted: 0.5 °F is 0.28 °C, and HA snaps
# input to step multiples, so a converted step would make round °C values
# unreachable.
_CELSIUS_TEMP_STEP = 0.1


def _display_bound(value_f: float, unit: str, temp: str, *, is_min: bool) -> float:
    if unit != "C":
        return value_f
    celsius = (value_f - 32) * 5 / 9 if temp == TEMP_ABSOLUTE else value_f * 5 / 9
    # Kill float representation noise before rounding to a tenth, so an exactly
    # convertible bound (e.g. 0 °F delta → 0 °C) never jumps a notch.
    scaled = round(celsius * 10, 6)
    rounded = math.ceil(scaled) if is_min else math.floor(scaled)
    return round(rounded / 10, 1)


@dataclass(frozen=True)
class DiscoveryEntity:
    """One HA entity: where its config goes and what the payload is."""

    topic: str
    payload: dict


def availability_topic(prefix: str) -> str:
    """LWT topic. ``online`` while connected, ``offline`` after an ungraceful drop."""
    return f"{prefix.strip('/')}/status"


def device_block(prefix: str, device: str, ident: str, display_name: str) -> dict:
    """The HA device every control of one room/thermostat groups under.

    ``ident`` must be the **canonical** identifier (room GUID, thermostat
    entity_id) so the device survives a rename. The system device ignores
    ``ident``: its identifier is exactly the ``via_device`` value every child
    publishes, and there is only ever one per instance. (These used to
    diverge — ``{prefix}_system_plenum`` vs ``{prefix}_system`` — so HA
    parented every room to an identifier no config ever claimed and showed
    "Connected via Unnamed device".)
    """
    if device == "system":
        identifier = f"{prefix}_system"
    else:
        identifier = f"{prefix}_{device}_{sanitize(ident)}"
    block = {
        "identifiers": [identifier],
        "name": display_name,
        "manufacturer": instance_title(prefix),
        "model": device.capitalize(),
    }
    if device != "system":
        block["via_device"] = f"{prefix}_system"
    return block


def _unique_id(prefix: str, device: str, ident: str, key: str, suffix: str = "") -> str:
    parts = [prefix, device, sanitize(ident), sanitize(key)]
    if suffix:
        parts.append(suffix)
    return "_".join(p for p in parts if p)


def _config_topic(discovery_prefix: str, component: str, unique_id: str) -> str:
    return f"{discovery_prefix.strip('/')}/{component}/{unique_id}/config"


def _temperature_unit_label(unit: str) -> str:
    return "°C" if unit == "C" else "°F"


def build_entities(
    control: Control,
    *,
    prefix: str,
    discovery_prefix: str,
    device: str,
    ident: str,
    topic_ident: str,
    device_info: dict,
    unit: str,
    topic_key: str | None = None,
    name_override: str | None = None,
) -> list[DiscoveryEntity]:
    """Build every HA entity one control contributes.

    A nullable or clearable control contributes two: the value entity and a
    ``Clear`` button, because HA's number/switch platforms have no way to
    express "unset" and #519 requires the clear to be reachable from the
    automation UI, not only from a raw topic.

    *ident* is the canonical id (drives ``unique_id``); *topic_ident* is what
    goes in the topic strings — always the id form for discovery.
    """
    key = topic_key or control.key
    name = name_override or control.name
    base: dict[str, Any] = {
        "device": device_info,
        "availability_topic": availability_topic(prefix),
        "payload_available": "online",
        "payload_not_available": "offline",
    }
    if control.icon:
        base["icon"] = control.icon

    entities: list[DiscoveryEntity] = []

    payload: dict[str, Any]
    if control.kind == KIND_BOOL:
        component = "switch"
        payload = {
            **base,
            "name": name,
            "unique_id": _unique_id(prefix, device, ident, key),
            "command_topic": command_topic(prefix, device, topic_ident, key, VERB_SET),
            "state_topic": state_topic(prefix, device, topic_ident, key),
            "payload_on": "ON",
            "payload_off": "OFF",
        }
    elif control.kind == KIND_NUMBER:
        component = "number"
        payload = {
            **base,
            "name": name,
            "unique_id": _unique_id(prefix, device, ident, key),
            "command_topic": command_topic(prefix, device, topic_ident, key, VERB_SET),
            "state_topic": state_topic(prefix, device, topic_ident, key),
            # "box" rather than a slider: these are precise setpoints, and a
            # slider over a 40–90 range makes half-degree accuracy fiddly.
            "mode": "box",
        }
        if control.min is not None:
            payload["min"] = (
                _display_bound(control.min, unit, control.temp, is_min=True)
                if control.temp is not None
                else control.min
            )
        if control.max is not None:
            payload["max"] = (
                _display_bound(control.max, unit, control.temp, is_min=False)
                if control.temp is not None
                else control.max
            )
        if control.step is not None:
            payload["step"] = (
                _CELSIUS_TEMP_STEP if control.temp is not None and unit == "C" else control.step
            )
        if control.temp is not None:
            payload["unit_of_measurement"] = _temperature_unit_label(unit)
            # Only an absolute reading is a temperature *measurement*; tagging a
            # delta (a deadband, an offset) with device_class temperature would
            # have HA re-convert it against its own unit system and mangle it.
            if control.temp == TEMP_ABSOLUTE:
                payload["device_class"] = "temperature"
        elif control.unit:
            payload["unit_of_measurement"] = control.unit
    elif control.kind == KIND_ENUM:
        component = "select"
        payload = {
            **base,
            "name": name,
            "unique_id": _unique_id(prefix, device, ident, key),
            "command_topic": command_topic(prefix, device, topic_ident, key, VERB_SET),
            "state_topic": state_topic(prefix, device, topic_ident, key),
            "options": list(control.options),
        }
    elif control.kind == KIND_DATETIME:
        component = "datetime"
        payload = {
            **base,
            "name": name,
            "unique_id": _unique_id(prefix, device, ident, key),
            "command_topic": command_topic(prefix, device, topic_ident, key, VERB_SET),
            "state_topic": state_topic(prefix, device, topic_ident, key),
        }
    else:  # KIND_ACTION — a button, no state
        payload = {
            **base,
            "name": name,
            "unique_id": _unique_id(prefix, device, ident, key),
            "command_topic": command_topic(prefix, device, topic_ident, key, VERB_CLEAR),
            "payload_press": "PRESS",
        }
        return [
            DiscoveryEntity(
                _config_topic(discovery_prefix, "button", str(payload["unique_id"])), payload
            )
        ]

    entities.append(
        DiscoveryEntity(
            _config_topic(discovery_prefix, component, str(payload["unique_id"])), payload
        )
    )

    if control.nullable or control.clearable:
        clear_uid = _unique_id(prefix, device, ident, key, "clear")
        clear_label = "Clear" if control.clearable and not control.nullable else "Inherit"
        entities.append(
            DiscoveryEntity(
                _config_topic(discovery_prefix, "button", clear_uid),
                {
                    **base,
                    "name": f"{name} ({clear_label})",
                    "unique_id": clear_uid,
                    "command_topic": command_topic(prefix, device, topic_ident, key, VERB_CLEAR),
                    "payload_press": "PRESS",
                },
            )
        )

    return entities


def removal_topics(entities: list[DiscoveryEntity]) -> list[str]:
    """Config topics to blank out to make HA forget these entities."""
    return [e.topic for e in entities]
