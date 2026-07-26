"""Building and parsing Plenum's MQTT topic tree (Issue #519).

Shape, under the resolved instance ``<prefix>``::

    <prefix>/room/<room_id|room_name>/<key>/set        → .../set/result
    <prefix>/room/<room_id|room_name>/<key>/clear      → .../clear/result
    <prefix>/room/<room_id|room_name>/<key>/state      (retained)
    <prefix>/room/<room_id|room_name>/schedule/<schedule_id>/set
    <prefix>/thermostat/<sanitized_entity_id>/<key>/set
    <prefix>/system/<key>/set

**Dual room addressing.** Every room-scoped topic is reachable by the room's
GUID *and* by its sanitised name. The id is canonical everywhere else in Plenum
(DB keys, REST paths, MCP, the frontend) and stays so; the name tree exists
purely because a name is what a human types into an automation. Raw retained
state is published under both, and a result echoes back on whichever segment
the incoming command actually used. HA Discovery only ever references the id
form, so a rename never disturbs HA's entity registry.
"""

from __future__ import annotations

from dataclasses import dataclass

ROOM = "room"
THERMOSTAT = "thermostat"
SYSTEM = "system"
SCHEDULE = "schedule"

VERB_SET = "set"
VERB_CLEAR = "clear"
STATE = "state"
RESULT = "result"

_COMMAND_VERBS = (VERB_SET, VERB_CLEAR)


@dataclass(frozen=True)
class ParsedCommand:
    """A command topic decomposed into its parts."""

    device: str  # room | thermostat | system
    ident: str  # room id-or-name, sanitised thermostat entity id, or "" for system
    key: str  # control key, e.g. "temp_offset" or "vacation_mode/return_at"
    verb: str  # set | clear
    schedule_id: str | None = None


def command_wildcards(prefix: str) -> list[str]:
    """The minimal subscription set covering every command topic.

    Two levels of ``#`` rather than one per control: the tree is a few hundred
    topics across rooms × controls, and re-subscribing on every room or schedule
    change would be far more churn than filtering in the parser. Result and
    state topics are excluded by :func:`parse_command` returning ``None``, so
    Plenum never reacts to its own retained publishes.
    """
    return [
        f"{prefix}/{ROOM}/#",
        f"{prefix}/{THERMOSTAT}/#",
        f"{prefix}/{SYSTEM}/#",
    ]


def parse_command(prefix: str, topic: str) -> ParsedCommand | None:
    """Decompose a command topic, or return ``None`` if it is not one.

    Rejects anything that is not exactly a ``.../set`` or ``.../clear`` — which
    covers our own ``.../state`` and ``.../<verb>/result`` publishes, so the
    broker echoing them back can never be mistaken for an inbound command.
    """
    parts = topic.strip("/").split("/")
    expected_prefix = prefix.strip("/").split("/")
    if parts[: len(expected_prefix)] != expected_prefix:
        return None
    parts = parts[len(expected_prefix) :]
    # device + at least one segment + verb
    if len(parts) < 2:
        return None
    verb = parts[-1]
    if verb not in _COMMAND_VERBS:
        return None
    device, rest = parts[0], parts[1:-1]

    if device == SYSTEM:
        if not rest:
            return None
        return ParsedCommand(device=SYSTEM, ident="", key="/".join(rest), verb=verb)

    if device in (ROOM, THERMOSTAT):
        if len(rest) < 2:
            return None
        ident, key_parts = rest[0], rest[1:]
        if device == ROOM and key_parts[0] == SCHEDULE:
            # room/<ident>/schedule/<schedule_id>/<verb>
            if len(key_parts) != 2:
                return None
            return ParsedCommand(
                device=ROOM,
                ident=ident,
                key=SCHEDULE,
                verb=verb,
                schedule_id=key_parts[1],
            )
        return ParsedCommand(device=device, ident=ident, key="/".join(key_parts), verb=verb)

    return None


def _base(prefix: str, device: str, ident: str, key: str) -> str:
    segments = [prefix.strip("/"), device]
    if ident:
        segments.append(ident)
    segments.append(key)
    return "/".join(segments)


def command_topic(prefix: str, device: str, ident: str, key: str, verb: str = VERB_SET) -> str:
    return f"{_base(prefix, device, ident, key)}/{verb}"


def state_topic(prefix: str, device: str, ident: str, key: str) -> str:
    return f"{_base(prefix, device, ident, key)}/{STATE}"


def result_topic(prefix: str, device: str, ident: str, key: str, verb: str) -> str:
    return f"{_base(prefix, device, ident, key)}/{verb}/{RESULT}"


def schedule_key(schedule_id: str) -> str:
    """Topic key for a per-schedule switch: ``schedule/<schedule_id>``."""
    return f"{SCHEDULE}/{schedule_id}"


def room_topic_root(prefix: str, ident: str) -> str:
    """Root of one room's subtree — used to retire a whole name alias on rename."""
    return f"{prefix.strip('/')}/{ROOM}/{ident}"
