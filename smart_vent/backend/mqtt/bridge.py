"""The MQTT runtime: subscribe, dispatch, publish (Issue #519).

The bridge owns no domain logic. It reads the world through the REST API,
renders it onto retained topics, and turns inbound commands back into REST
calls. Two injected collaborators keep it testable without a broker or a socket:

``dispatch``  — ``(method, path, body) -> (status, payload)``. In production this
is a loopback HTTP call carrying the per-boot internal token, exactly like
``mcp_http.dispatch_tool``. In tests it can be an aiohttp ``TestClient``, so a
command genuinely travels MQTT → REST → DB.

``transport`` — the broker connection, behind :class:`MqttTransport`. The fake
in the test suite records publishes and feeds messages in.

**Retiring topics.** MQTT has no "delete a subtree", so every retained topic the
bridge has ever published is tracked. Each sync diffs the topic set it *should*
have against the set it *does* have and blanks the difference. Deleted rooms,
deleted schedules, and — the case #519 calls out — a renamed room's stale
name-alias topics all fall out of that one mechanism rather than needing
bespoke cleanup paths.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..units import from_f, from_f_delta
from . import commands, discovery, topics
from .commands import CommandError, RestCall
from .config import MqttConfig
from .naming import sanitize, sanitize_entity_id
from .registry import (
    DEVICE_ROOM,
    DEVICE_SYSTEM,
    DEVICE_THERMOSTAT,
    ROOM_CONTROLS,
    SCHEDULE_CONTROL,
    SYSTEM_CONTROLS,
    TEMP_ABSOLUTE,
    THERMOSTAT_CONTROLS,
    Control,
    control_for,
)

log = logging.getLogger(__name__)

# How often the world is re-read when nothing has poked us. Config knobs change
# rarely and every Plenum-side write pokes the bridge directly, so this only
# has to catch changes made by something that does not (a direct DB edit, an
# engine-driven expiry).
DEFAULT_REFRESH_SECONDS = 30.0

# Backoff bounds for the reconnect loop.
_RECONNECT_MIN = 2.0
_RECONNECT_MAX = 60.0

# How long to sit idle while the runtime toggle is off. The toggle also pokes
# `request_sync`, so this is only a backstop, not the normal response time.
_DISABLED_POLL_SECONDS = 5.0

Dispatch = Callable[[str, str, dict | None], Awaitable[tuple[int, Any]]]


class _Disabled(Exception):
    """Raised internally to end a session when the runtime toggle goes off."""


class MqttTransport(Protocol):
    """The slice of an MQTT client the bridge needs."""

    async def publish(self, topic: str, payload: str, retain: bool = False) -> None: ...

    async def subscribe(self, topic: str) -> None: ...

    async def unsubscribe(self, topic: str) -> None: ...

    def messages(self) -> AsyncIterator[tuple[str, str, bool]]: ...


@dataclass
class Snapshot:
    """One consistent read of everything MQTT mirrors.

    ``failed`` names the reads that errored while building this snapshot (their
    sections hold defaults). A partial snapshot is still good enough to publish
    the sections that *did* load, but it must never drive deletions: a topic
    missing from it may be missing because the read failed, not because the
    thing is gone.
    """

    unit: str = "F"
    rooms: list[dict] = field(default_factory=list)
    thermostats: list[dict] = field(default_factory=list)
    schedules: dict[str, list[dict]] = field(default_factory=dict)
    holds: dict[str, float | None] = field(default_factory=dict)
    system_enabled: bool = True
    vacation: dict = field(default_factory=dict)
    eco_suspend: dict[str, str] = field(default_factory=dict)
    failed: set[str] = field(default_factory=set)

    @property
    def partial(self) -> bool:
        return bool(self.failed)

    def room_by_ident(self, ident: str) -> dict | None:
        """Resolve a room by GUID or by sanitised name — #519's dual addressing."""
        for room in self.rooms:
            if room.get("id") == ident:
                return room
        for room in self.rooms:
            if sanitize(str(room.get("name", ""))) == ident:
                return room
        return None

    def thermostat_by_ident(self, ident: str) -> dict | None:
        for thermostat in self.thermostats:
            entity_id = str(thermostat.get("thermostat_entity_id", ""))
            if entity_id == ident or sanitize_entity_id(entity_id) == ident:
                return thermostat
        return None


class MqttBridge:
    """Mirrors Plenum onto MQTT and applies inbound commands."""

    def __init__(
        self,
        config: MqttConfig,
        dispatch: Dispatch,
        transport_factory: Callable[[], Any],
        *,
        is_enabled: Callable[[], bool] | None = None,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    ) -> None:
        self._config = config
        self._dispatch = dispatch
        self._transport_factory = transport_factory
        # Runtime on/off, owned by the user from the Settings page. Defaults to
        # "always on" so tests can drive the bridge without a scheduler.
        self._is_enabled = is_enabled or (lambda: True)
        self._refresh_seconds = refresh_seconds
        self._transport: MqttTransport | None = None
        self._last_error: str | None = None
        # Every retained topic we have published → its last payload. Drives both
        # change suppression and the retire-the-difference sweep.
        self._retained: dict[str, str] = {}
        self._wake = asyncio.Event()
        self._stopped = False
        # Active display unit, refreshed from each snapshot. State-side °F →
        # display conversion reads it; commands never convert (the REST write
        # boundary owns that direction).
        self._config_unit = "F"
        # Reconcile-at-connect: retained topics of ours the broker replayed on
        # subscribe. Anything here that the first full sync does not want was
        # left behind by an earlier run (a room deleted while disconnected, a
        # stale discovery config, a mistakenly retained command) and is blanked
        # once per session. `_retained` alone cannot catch these — it is this
        # process's memory, and the stale topics predate this process.
        self._broker_retained: set[str] = set()
        self._reconciled = True

    # -- lifecycle --------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._transport is not None

    @property
    def last_error(self) -> str | None:
        """Why the last connection attempt failed, for the Settings panel."""
        return self._last_error

    def request_sync(self) -> None:
        """Ask for a resync at the next opportunity. Safe from any coroutine."""
        self._wake.set()

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()

    async def _idle(self, seconds: float) -> None:
        """Sleep, but wake early if something calls :meth:`request_sync`."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        self._wake.clear()

    async def run(self) -> None:
        """Connect, serve, and reconnect until :meth:`stop`.

        Nothing here is fatal. A broker that is down, misconfigured, or briefly
        unreachable must never take the add-on with it — HVAC control does not
        depend on MQTT, so the loop just backs off and tries again.
        """
        delay = _RECONNECT_MIN
        while not self._stopped:
            if not self._is_enabled():
                await self._idle(_DISABLED_POLL_SECONDS)
                continue
            try:
                async with self._transport_factory() as transport:
                    self._transport = transport
                    self._last_error = None
                    # A fresh connection knows nothing about what the broker
                    # still retains from the last one, so forget our record and
                    # republish everything rather than skipping topics whose
                    # value happens not to have changed.
                    self._retained.clear()
                    await self._serve(transport)
                delay = _RECONNECT_MIN
            except asyncio.CancelledError:
                raise
            except _Disabled:
                log.info("MQTT bridge disabled — disconnected")
                delay = _RECONNECT_MIN
            except Exception as exc:
                if self._stopped:
                    break
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.exception("MQTT connection lost — reconnecting in %.0fs", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX)
            finally:
                self._transport = None

    async def _serve(self, transport: MqttTransport) -> None:
        """One connected session: announce, sync, then serve until it drops."""
        await transport.publish(discovery.availability_topic(self._config.prefix), "online", True)
        for wildcard in topics.command_wildcards(self._config.prefix):
            await transport.subscribe(wildcard)
        # Also watch our own discovery configs for the reconcile sweep: the
        # broker replays every retained topic on subscribe, which is the only
        # way to learn what an *earlier* run left behind. Unsubscribed again as
        # soon as the sweep has run.
        await transport.subscribe(self._discovery_wildcard())
        self._broker_retained.clear()
        self._reconciled = False
        log.info("MQTT connected — serving topic tree under %r", self._config.prefix)

        await self.sync()

        reader = asyncio.create_task(self._read_loop(transport))
        refresher = asyncio.create_task(self._refresh_loop())
        try:
            done, _ = await asyncio.wait({reader, refresher}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()  # re-raise whatever ended the session
        finally:
            for task in (reader, refresher):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    def _discovery_wildcard(self) -> str:
        return f"{self._config.discovery_prefix.strip('/')}/+/+/config"

    async def _read_loop(self, transport: MqttTransport) -> None:
        async for topic, payload, retained in transport.messages():
            if self._stopped:
                return
            if retained:
                # A retained message is a broker replay, not a live command.
                # Executing one would re-apply a stale command on every
                # reconnect — so it is only ever fed to the reconcile sweep,
                # which will *clear* a retained command rather than run it.
                self._note_broker_retained(topic, payload)
                continue
            try:
                await self.handle_message(topic, payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One malformed command must never drop the connection.
                log.exception("Failed to handle MQTT message on %s", topic)

    def _note_broker_retained(self, topic: str, payload: str) -> None:
        if self._reconciled or not payload:
            return
        prefix = self._config.prefix.strip("/")
        ours = topic.startswith(
            (
                f"{prefix}/{topics.ROOM}/",
                f"{prefix}/{topics.THERMOSTAT}/",
                f"{prefix}/{topics.SYSTEM}/",
            )
        )
        if ours or self._is_our_discovery_config(topic):
            self._broker_retained.add(topic)

    def _is_our_discovery_config(self, topic: str) -> bool:
        """Whether *topic* is one of OUR discovery configs.

        The config wildcard sees every integration's discovery topics, so the
        object id must carry our exact device prefix — `plenum_room_…` never
        matches a `plenum_beta` install's `plenum_beta_room_…` and vice versa.
        """
        parts = topic.split("/")
        if len(parts) != 4 or parts[3] != "config":
            return False
        if parts[0] != self._config.discovery_prefix.strip("/"):
            return False
        prefix = self._config.prefix
        return parts[2].startswith(
            (
                f"{prefix}_{topics.ROOM}_",
                f"{prefix}_{topics.THERMOSTAT}_",
                f"{prefix}_{topics.SYSTEM}_",
            )
        )

    async def _refresh_loop(self) -> None:
        while not self._stopped:
            await self._idle(self._refresh_seconds)
            if self._stopped:
                return
            # The Settings toggle is checked here rather than in the read loop:
            # this is the one part of a session guaranteed to run on a timer
            # even when the broker is silent.
            if not self._is_enabled():
                raise _Disabled
            try:
                snapshot = await self.sync()
                if snapshot is not None and not snapshot.partial and not self._reconciled:
                    await self._reconcile_stale()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("MQTT state sync failed")

    async def _reconcile_stale(self) -> None:
        """Blank retained topics an earlier run left behind (once per session).

        Runs on the first refresh after connect — by then the broker's retained
        replay has long been consumed — and only off a FULL snapshot: a partial
        one is missing sections whose topics would look stale but are live.
        """
        transport = self._transport
        if transport is None:
            return
        stale = self._broker_retained - set(self._retained)
        for topic in sorted(stale):
            await transport.publish(topic, "", True)
        if stale:
            log.info("MQTT reconcile: retired %d stale retained topic(s)", len(stale))
        self._reconciled = True
        self._broker_retained.clear()
        await transport.unsubscribe(self._discovery_wildcard())

    # -- reading the world ------------------------------------------------

    async def build_snapshot(self) -> Snapshot:
        """Read everything MQTT mirrors, recording which reads failed.

        A failed read fills its section with a default and names itself in
        ``snapshot.failed`` — it must never pass for "the thing is gone", or a
        transient 500 would blank retained state and delete discovery entities
        (the retire sweep and the reconcile sweep both refuse partial
        snapshots).
        """
        failed: set[str] = set()

        async def read(key: str, path: str, default):
            status, payload = await self._dispatch("GET", path, None)
            if status >= 400:
                log.warning("MQTT sync: GET %s returned %s", path, status)
                failed.add(key)
                return default
            return payload

        settings = await read("settings", "/api/settings", {})
        rooms = await read("rooms", "/api/rooms", [])
        thermostats = await read("thermostats", "/api/thermostats", [])
        system = await read("system", "/api/system/status", {})

        # When the settings read failed, keep the last-known display unit
        # rather than defaulting to °F — republishing a °C install's state as
        # °F numbers for one cycle would be a worse lie than staleness.
        unit = str(settings.get("temperature_unit") or "F")
        if "settings" in failed:
            unit = self._config_unit

        snapshot = Snapshot(
            unit=unit,
            rooms=list(rooms or []),
            thermostats=list(thermostats or []),
            system_enabled=bool(system.get("enabled", True)),
            vacation=dict(settings.get("vacation_mode") or {}),
            eco_suspend=dict(settings.get("eco_suspend") or {}),
            failed=failed,
        )

        for room in snapshot.rooms:
            room_id = str(room.get("id"))
            snapshot.schedules[room_id] = list(
                await read(f"schedules:{room_id}", f"/api/rooms/{room_id}/schedules", []) or []
            )

        room_ids = [str(r.get("id")) for r in snapshot.rooms]
        if room_ids:
            status, statuses = await self._dispatch(
                "POST", "/api/rooms/active-status", {"room_ids": room_ids}
            )
            if status < 400 and isinstance(statuses, dict):
                for room_id, info in statuses.items():
                    # Only an override counts as a "hold". A schedule or presence
                    # target is not something this entity set, and reporting it
                    # would have an automation believe a hold is in place.
                    snapshot.holds[room_id] = (
                        info.get("target_temp") if info.get("source") == "override" else None
                    )
            else:
                log.warning("MQTT sync: POST /api/rooms/active-status returned %s", status)
                failed.add("active_status")
        return snapshot

    # -- rendering state --------------------------------------------------

    def _display(self, control: Control, value_f: Any) -> float | None:
        """Convert a stored °F value into the active display unit.

        Absolute and delta conversions differ by the 32° offset — using the
        wrong one is the #231 class of bug, so the registry carries which is
        which and this is the only place state-side conversion happens.
        """
        if value_f is None:
            return None
        if control.temp == TEMP_ABSOLUTE:
            return float(from_f(float(value_f), self._config_unit))
        return float(from_f_delta(float(value_f), self._config_unit))

    def _resolve(self, control: Control, source: dict, fallback: dict | None) -> Any:
        """The effective value for a control: own value, else inherited.

        #519 specifies state topics carry the value *actually in use*, so a room
        whose ``deadband_override`` is null reports its thermostat's deadband.
        An automation cannot tell "explicit" from "inherited" from the state
        topic alone — an accepted v1 limitation.
        """
        value = source.get(control.field)
        inherit = control.inherit_field
        if value is None and inherit is not None and fallback is not None:
            value = fallback.get(inherit)
        return value

    def _state_payload(self, control: Control, value: Any) -> str:
        if control.temp is not None and value is not None:
            value = self._display(control, value)
        return commands.encode_value(control, value)

    def desired_state(self, snapshot: Snapshot) -> dict[str, str]:
        """Every retained state topic and its payload for this snapshot.

        Sections whose read failed are OMITTED, not defaulted: with the retire
        sweep also skipped on a partial snapshot, an omitted topic simply keeps
        its previous retained value instead of being blanked or overwritten
        with a guess.
        """
        self._config_unit = snapshot.unit
        prefix = self._config.prefix
        out: dict[str, str] = {}
        thermostats = {str(t.get("thermostat_entity_id")): t for t in snapshot.thermostats}

        for room in snapshot.rooms:
            room_id = str(room.get("id"))
            parent = thermostats.get(str(room.get("thermostat_entity_id")))
            for ident in self._room_idents(room):
                for control in ROOM_CONTROLS:
                    if not control.has_state:
                        continue
                    if control.special == "hold_set" and "active_status" in snapshot.failed:
                        continue
                    room_value: Any = (
                        snapshot.holds.get(room_id)
                        if control.special == "hold_set"
                        else self._resolve(control, room, parent)
                    )
                    out[topics.state_topic(prefix, DEVICE_ROOM, ident, control.key)] = (
                        self._state_payload(control, room_value)
                    )
                for schedule in snapshot.schedules.get(room_id, []):
                    key = topics.schedule_key(str(schedule.get("id")))
                    out[topics.state_topic(prefix, DEVICE_ROOM, ident, key)] = commands.encode_bool(
                        bool(schedule.get("enabled", True))
                    )

        for thermostat in snapshot.thermostats:
            entity_id = str(thermostat.get("thermostat_entity_id"))
            ident = sanitize_entity_id(entity_id)
            suspended_until = snapshot.eco_suspend.get(entity_id) or thermostat.get(
                "eco_suspend_until"
            )
            for control in THERMOSTAT_CONTROLS:
                if not control.has_state:
                    continue
                thermostat_value: Any
                if control.special == "eco_suspend_toggle":
                    thermostat_value = bool(suspended_until)
                elif control.special == "eco_suspend_set":
                    thermostat_value = suspended_until
                else:
                    thermostat_value = self._resolve(control, thermostat, None)
                out[topics.state_topic(prefix, DEVICE_THERMOSTAT, ident, control.key)] = (
                    self._state_payload(control, thermostat_value)
                )

        for control in SYSTEM_CONTROLS:
            if control.special == "system_enabled":
                if "system" in snapshot.failed:
                    continue
                value: Any = snapshot.system_enabled
            elif "settings" in snapshot.failed:
                continue
            elif control.special == "vacation_toggle":
                value = bool(snapshot.vacation.get("enabled"))
            else:
                value = snapshot.vacation.get("return_at")
            out[topics.state_topic(prefix, DEVICE_SYSTEM, "", control.key)] = self._state_payload(
                control, value
            )

        return out

    def _room_idents(self, room: dict) -> list[str]:
        """The id and (when usable) sanitised-name segments a room answers on."""
        room_id = str(room.get("id"))
        idents = [room_id]
        name_ident = sanitize(str(room.get("name", "")))
        if name_ident and name_ident != room_id:
            idents.append(name_ident)
        return idents

    # -- rendering discovery ----------------------------------------------

    def desired_discovery(self, snapshot: Snapshot) -> dict[str, str]:
        """Every HA Discovery config topic and its JSON payload."""
        self._config_unit = snapshot.unit
        if not self._config.discovery:
            return {}
        prefix = self._config.prefix
        dprefix = self._config.discovery_prefix
        out: dict[str, str] = {}

        def add(entities):
            for entity in entities:
                out[entity.topic] = json.dumps(entity.payload, sort_keys=True)

        system_device = discovery.device_block(prefix, DEVICE_SYSTEM, "plenum", "Plenum System")
        for control in SYSTEM_CONTROLS:
            add(
                discovery.build_entities(
                    control,
                    prefix=prefix,
                    discovery_prefix=dprefix,
                    device=DEVICE_SYSTEM,
                    ident="",
                    topic_ident="",
                    device_info=system_device,
                    unit=snapshot.unit,
                )
            )

        for room in snapshot.rooms:
            room_id = str(room.get("id"))
            device_info = discovery.device_block(
                prefix, DEVICE_ROOM, room_id, f"Plenum {room.get('name') or room_id}"
            )
            for control in ROOM_CONTROLS:
                add(
                    discovery.build_entities(
                        control,
                        prefix=prefix,
                        discovery_prefix=dprefix,
                        device=DEVICE_ROOM,
                        ident=room_id,
                        topic_ident=room_id,
                        device_info=device_info,
                        unit=snapshot.unit,
                    )
                )
            # Schedules come and go at runtime, so their entities are generated
            # per schedule and retired by the same diff that retires a room.
            for schedule in snapshot.schedules.get(room_id, []):
                schedule_id = str(schedule.get("id"))
                label = schedule.get("name") or schedule_id
                add(
                    discovery.build_entities(
                        SCHEDULE_CONTROL,
                        prefix=prefix,
                        discovery_prefix=dprefix,
                        device=DEVICE_ROOM,
                        ident=room_id,
                        topic_ident=room_id,
                        device_info=device_info,
                        unit=snapshot.unit,
                        topic_key=topics.schedule_key(schedule_id),
                        name_override=f"Schedule: {label}",
                    )
                )

        for thermostat in snapshot.thermostats:
            entity_id = str(thermostat.get("thermostat_entity_id"))
            ident = sanitize_entity_id(entity_id)
            device_info = discovery.device_block(
                prefix,
                DEVICE_THERMOSTAT,
                entity_id,
                f"Plenum {thermostat.get('name') or entity_id}",
            )
            for control in THERMOSTAT_CONTROLS:
                add(
                    discovery.build_entities(
                        control,
                        prefix=prefix,
                        discovery_prefix=dprefix,
                        device=DEVICE_THERMOSTAT,
                        ident=entity_id,
                        topic_ident=ident,
                        device_info=device_info,
                        unit=snapshot.unit,
                    )
                )

        return out

    # -- publishing -------------------------------------------------------

    async def sync(self) -> Snapshot | None:
        """Re-read the world and reconcile every retained topic with it."""
        transport = self._transport
        if transport is None:
            return None
        snapshot = await self.build_snapshot()
        desired = {**self.desired_discovery(snapshot), **self.desired_state(snapshot)}

        for topic, payload in desired.items():
            if self._retained.get(topic) != payload:
                await transport.publish(topic, payload, True)
                self._retained[topic] = payload

        # Anything we published before and no longer want — a deleted room, a
        # deleted schedule, a renamed room's old name alias — is retired by
        # blanking its retained payload. NEVER off a partial snapshot: a topic
        # absent because a read failed is not a topic whose subject was
        # deleted, and retiring a discovery config deletes the HA entity.
        if snapshot.partial:
            log.warning(
                "MQTT sync was partial (failed reads: %s) — skipping the retire sweep",
                ", ".join(sorted(snapshot.failed)),
            )
            return snapshot
        for topic in [t for t in self._retained if t not in desired]:
            await transport.publish(topic, "", True)
            del self._retained[topic]
        return snapshot

    # -- handling commands ------------------------------------------------

    async def handle_message(self, topic: str, payload: str) -> None:
        """Apply one inbound command and publish its result."""
        parsed = topics.parse_command(self._config.prefix, topic)
        if parsed is None:
            return  # our own state/result publish, or an unrelated topic

        verb = commands.verb_of(parsed.verb)
        try:
            call, control = await self._resolve_command(parsed, payload)
        except CommandError as exc:
            await self._publish_result(parsed, verb, False, str(exc))
            return

        status, body = await self._dispatch(call.method, call.path, call.body)
        if status >= 400:
            await self._publish_result(parsed, verb, False, _error_text(body, status))
            return

        await self._publish_result(parsed, verb, True, None)
        log.info("MQTT command applied: %s %s", call.method, call.path)
        # The write changed something; re-render immediately rather than waiting
        # out the refresh interval.
        self.request_sync()

    async def _resolve_command(self, parsed, payload: str) -> tuple[RestCall, Control]:
        """Map a parsed topic + payload onto a REST call.

        Resolution needs a snapshot only to turn an addressing segment (a room
        name alias, a sanitised thermostat entity id) back into the real id.
        """
        if parsed.device == DEVICE_SYSTEM:
            control = control_for(DEVICE_SYSTEM, parsed.key)
            if control is None:
                raise CommandError(f"unknown system control {parsed.key!r}")
            return commands.build_request(
                control, parsed.verb, payload, device=DEVICE_SYSTEM, resolved_id=""
            ), control

        snapshot = await self.build_snapshot()

        if parsed.device == DEVICE_ROOM:
            room = snapshot.room_by_ident(parsed.ident)
            if room is None:
                raise CommandError(f"unknown room {parsed.ident!r}")
            control = (
                SCHEDULE_CONTROL
                if parsed.schedule_id is not None
                else control_for(DEVICE_ROOM, parsed.key)
            )
            if control is None:
                raise CommandError(f"unknown room control {parsed.key!r}")
            return commands.build_request(
                control,
                parsed.verb,
                payload,
                device=DEVICE_ROOM,
                resolved_id=str(room["id"]),
                schedule_id=parsed.schedule_id,
            ), control

        thermostat = snapshot.thermostat_by_ident(parsed.ident)
        if thermostat is None:
            raise CommandError(f"unknown thermostat {parsed.ident!r}")
        control = control_for(DEVICE_THERMOSTAT, parsed.key)
        if control is None:
            raise CommandError(f"unknown thermostat control {parsed.key!r}")
        return commands.build_request(
            control,
            parsed.verb,
            payload,
            device=DEVICE_THERMOSTAT,
            resolved_id=str(thermostat["thermostat_entity_id"]),
        ), control

    async def _publish_result(self, parsed, verb: str, ok: bool, error: str | None) -> None:
        """Publish the outcome of every attempt, success or failure.

        Non-retained, and echoed on whichever segment — id or name — the command
        actually arrived on, so an automation subscribing by name hears back by
        name.
        """
        transport = self._transport
        if transport is None:
            return
        key = (
            topics.schedule_key(parsed.schedule_id)
            if parsed.schedule_id is not None
            else parsed.key
        )
        topic = topics.result_topic(self._config.prefix, parsed.device, parsed.ident, key, verb)
        body: dict[str, Any] = {"ok": ok}
        if error:
            body["error"] = error
        await transport.publish(topic, json.dumps(body), False)


def _error_text(body: Any, status: int) -> str:
    """Pull a user-safe message out of a REST error response.

    Route handlers already return sanitised messages (never raw exception
    detail — CLAUDE.md / CWE-209), so echoing one onto a result topic is safe.
    """
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"])
    return f"HTTP {status}"
