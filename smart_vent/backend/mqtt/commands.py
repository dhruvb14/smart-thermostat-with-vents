"""Turning an MQTT payload into a REST call (Issue #519).

Nothing here validates *domain* rules — no range checks, no unit conversion, no
"is this thermostat real". That is the route handler's job and it stays there,
so MQTT, REST, and MCP cannot drift apart. This module only decodes the wire
payload (``ON`` → ``True``, ``""`` → ``None``, an ISO-8601 string → a UTC
instant) and picks the endpoint. Anything it rejects is a payload that could not
be turned into a request at all.

Temperature values pass through **unconverted**: MQTT payloads arrive in the
system's active display unit, exactly like a form submission, and the REST write
boundary's ``_to_f`` / ``_delta_to_f`` converts once. Converting here would be
the #231 double-conversion, on a new transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .. import tz
from .registry import (
    KIND_ACTION,
    KIND_BOOL,
    KIND_DATETIME,
    KIND_ENUM,
    KIND_NUMBER,
    Control,
)
from .topics import VERB_CLEAR, VERB_SET

# HA's switch platform uses these by default; we accept the obvious synonyms so
# a hand-written `mqtt.publish` with "true" or "1" is not a silent no-op.
_TRUE = {"on", "true", "1", "yes"}
_FALSE = {"off", "false", "0", "no"}


class CommandError(Exception):
    """A payload that cannot be turned into a request. Message is user-safe."""


@dataclass(frozen=True)
class RestCall:
    """The loopback REST request a command resolves to."""

    method: str
    path: str
    body: dict | None = None


@dataclass(frozen=True)
class Target:
    """Resolved identity for the device a command addresses.

    ``ident`` is what the topic said (a room GUID, a sanitised room name, or a
    sanitised thermostat entity id); ``resolved`` is the real identifier the
    REST path needs.
    """

    resolved: str


def parse_bool(payload: str) -> bool:
    text = payload.strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise CommandError(f"expected ON or OFF, got {payload!r}")


def parse_number(payload: str) -> float:
    try:
        return float(payload.strip())
    except (TypeError, ValueError):
        raise CommandError(f"expected a number, got {payload!r}") from None


def parse_datetime(payload: str) -> datetime:
    """Parse an ISO-8601 instant into UTC.

    A naive value is read in the add-on's configured local timezone, not UTC: a
    human typing ``2026-08-01T18:00`` into an automation means six in the
    evening where they live. HA's own MQTT ``datetime`` entity always publishes
    an offset, so this only affects hand-written payloads.
    """
    text = payload.strip()
    if not text:
        raise CommandError("expected an ISO-8601 datetime, got an empty payload")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise CommandError(f"expected an ISO-8601 datetime, got {payload!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz.get_timezone())
    return parsed.astimezone(UTC)


def decode_value(control: Control, payload: str):
    """Decode a ``.../set`` payload into the value the REST body expects.

    Returns ``None`` for the empty payload on a nullable control — #519's
    "clear to inherit" shorthand on the set topic.
    """
    if control.nullable and payload.strip() == "":
        return None
    if control.kind == KIND_BOOL:
        return parse_bool(payload)
    if control.kind == KIND_NUMBER:
        return parse_number(payload)
    if control.kind == KIND_ENUM:
        value = payload.strip()
        if value not in control.options:
            raise CommandError(f"expected one of {', '.join(control.options)}, got {value!r}")
        return value
    if control.kind == KIND_DATETIME:
        return parse_datetime(payload).isoformat()
    if control.kind == KIND_ACTION:
        return None
    raise CommandError(f"unsupported control kind {control.kind!r}")


def _room_path(room_id: str, *suffix: str) -> str:
    return "/".join(["/api/rooms", room_id, *suffix])


def _thermostat_path(entity_id: str, *suffix: str) -> str:
    return "/".join(["/api/thermostats", entity_id, *suffix])


def build_request(
    control: Control,
    verb: str,
    payload: str,
    *,
    device: str,
    resolved_id: str,
    schedule_id: str | None = None,
) -> RestCall:
    """Resolve one command into a :class:`RestCall`.

    *resolved_id* is the real room GUID or thermostat ``entity_id`` — the caller
    has already mapped a name alias or sanitised entity id back to it.
    """
    if control.special is not None:
        return _build_special(control, verb, payload, resolved_id, schedule_id)

    # Plain field write on the device's own resource. A `clear` is just a write
    # of null, which is exactly what "inherit" means on these fields.
    value = None if verb == VERB_CLEAR else decode_value(control, payload)
    if verb == VERB_CLEAR and not control.nullable:
        raise CommandError(f"{control.key} cannot be cleared")
    body = {control.field: value}
    if device == "room":
        return RestCall("PUT", _room_path(resolved_id), body)
    return RestCall("PUT", _thermostat_path(resolved_id), body)


def _build_special(
    control: Control,
    verb: str,
    payload: str,
    resolved_id: str,
    schedule_id: str | None,
) -> RestCall:
    special = control.special

    if special == "presence_clear":
        return RestCall("DELETE", _room_path(resolved_id, "presence", "holdover"))

    if special == "hold_set":
        if verb == VERB_CLEAR:
            return RestCall("DELETE", _room_path(resolved_id, "override"))
        # duration_hours omitted → the API's 2h default, and the write fully
        # replaces any existing hold. Matches REST exactly (#519).
        return RestCall(
            "POST", _room_path(resolved_id, "override"), {"target_temp": parse_number(payload)}
        )

    if special == "schedule_enabled":
        if schedule_id is None:
            raise CommandError("schedule command is missing a schedule id")
        return RestCall(
            "PUT",
            _room_path(resolved_id, "schedules", schedule_id),
            {"enabled": parse_bool(payload)},
        )

    if special == "eco_suspend_set":
        return RestCall(
            "POST",
            _thermostat_path(resolved_id, "eco-suspend"),
            {"resume_at": parse_datetime(payload).isoformat()},
        )

    if special == "eco_suspend_toggle":
        if parse_bool(payload):
            # There is no resume time in an ON, and the API requires a future
            # one. Mirrors the vacation switch's ON rejection.
            raise CommandError(
                "set eco_suspend_until to a future date/time to suspend Eco Mode; "
                "this switch only turns a suspension off"
            )
        return RestCall("DELETE", _thermostat_path(resolved_id, "eco-suspend"))

    if special == "vacation_return_at":
        return RestCall(
            "POST",
            "/api/settings/vacation-mode",
            {"return_at": parse_datetime(payload).isoformat()},
        )

    if special == "vacation_toggle":
        if parse_bool(payload):
            raise CommandError(
                "set vacation_mode/return_at to a future date/time to enable vacation mode; "
                "this switch only turns it off"
            )
        return RestCall("DELETE", "/api/settings/vacation-mode")

    if special == "system_enabled":
        return RestCall("POST", "/api/system/enabled", {"enabled": parse_bool(payload)})

    raise CommandError(f"unsupported control {control.key!r}")


def encode_bool(value: bool) -> str:
    return "ON" if value else "OFF"


def encode_value(control: Control, value) -> str:
    """Render a resolved value for the retained state topic.

    ``None`` becomes the empty payload, which HA reads as "unknown" — the honest
    rendering for a nullable field with nothing set and no inherited value to
    show. Callers resolve inheritance *before* calling this (#519: a state topic
    carries the effective value actually in use).
    """
    if value is None:
        return ""
    if control.kind == KIND_BOOL:
        return encode_bool(bool(value))
    if control.kind == KIND_NUMBER:
        number = float(value)
        # Trim a trailing .0 so an HA number entity shows "70", not "70.0".
        return str(int(number)) if number.is_integer() else str(round(number, 2))
    return str(value)


def verb_of(parsed_verb: str) -> str:
    """Normalise a parsed verb, defaulting anything unknown to ``set``."""
    return VERB_CLEAR if parsed_verb == VERB_CLEAR else VERB_SET
