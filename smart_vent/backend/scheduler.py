"""
Central scheduler.

- Runs a 60-second tick for every thermostat zone's CycleEngine
- Listens to HA state changes and triggers targeted ticks
- Manages the mapping of thermostats → CycleEngine instances
- Owns the system_enabled flag (persisted to DB)
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import db, tz
from .engine.cycle_engine import CycleEngine
from .engine.vent_controller import VentController
from .event_logger import EventLogger
from .ha_client import HAClient

log = logging.getLogger(__name__)

BroadcastFn = Callable[[str, dict], Coroutine]

# A single engine tick is retried a few times before giving up, so a transient
# failure — most likely `sqlite3.OperationalError: database is locked` while the
# MCP server briefly holds a write lock on the shared DB file — doesn't silently
# drop a whole 60s control cycle for the zone. (Issue #286)
_TICK_MAX_ATTEMPTS = 3
_TICK_RETRY_BACKOFF_S = 0.2


class Scheduler:
    def __init__(
        self,
        ha: HAClient,
        db_path: str,
        broadcast: BroadcastFn | None = None,
        event_logger: EventLogger | None = None,
    ) -> None:
        self._ha = ha
        self._db_path = db_path
        self._broadcast = broadcast
        self._event_logger = event_logger
        self._vent_ctrl: VentController | None = None
        self._engines: dict[str, CycleEngine] = {}
        self._apscheduler = AsyncIOScheduler()
        self._db_conn: aiosqlite.Connection = None  # type: ignore[assignment]
        self._system_enabled: bool = True
        self._dev_mode: bool = False
        self._active_unit: str = "F"
        self._unit_override: str = ""  # non-empty when locked by env var / config
        self._vacation_mode: bool = False
        self._vacation_return_at: datetime | None = None
        # Strong references to fire-and-forget background tasks. The event loop
        # only holds weak references, so without this a task can be
        # garbage-collected mid-await and silently never finish (Issue #304).
        self._bg_tasks: set[asyncio.Task] = set()

    def _spawn_bg(self, coro: Coroutine) -> asyncio.Task:
        """Create a background task and keep a strong reference until it
        completes, so it cannot be garbage-collected while still running."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    # ------------------------------------------------------------------
    # Startup / shutdown
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._db_conn = await aiosqlite.connect(self._db_path)
        self._db_conn.row_factory = aiosqlite.Row
        await db.init_db(self._db_conn)

        # Load persisted flags
        val = await db.get_system_setting(self._db_conn, "system_enabled", "1")
        self._system_enabled = val == "1"
        dev_val = await db.get_system_setting(self._db_conn, "developer_mode", "0")
        self._dev_mode = dev_val == "1"
        vac_val = await db.get_system_setting(self._db_conn, "vacation_mode_enabled", "0")
        self._vacation_mode = vac_val == "1"
        vac_return = await db.get_system_setting(self._db_conn, "vacation_mode_return_at", "")
        self._vacation_return_at = datetime.fromisoformat(vac_return) if vac_return else None

        # Temperature unit: env var / add-on config wins; otherwise restore last-known
        self._unit_override = os.environ.get("TEMPERATURE_UNIT", "").upper()
        if self._unit_override in ("F", "C"):
            self._active_unit = self._unit_override
            await db.set_system_setting(self._db_conn, "temperature_unit", self._active_unit)
        else:
            self._active_unit = await db.get_system_setting(self._db_conn, "temperature_unit", "F")
            # Resolve the real unit from HA once it connects and overwrite the DB.
            self._spawn_bg(self._startup_resolve_unit())

        # Wire logger's DB connection
        if self._event_logger:
            self._event_logger.set_conn(self._db_conn)
            await self._event_logger.log(
                "info",
                "system",
                f"Scheduler started (system {'enabled' if self._system_enabled else 'disabled'}"
                f"{', dev mode ON' if self._dev_mode else ''})",
            )

        # Wire dev_mode into HA client
        self._ha.dev_mode = self._dev_mode
        self._ha._dev_logger = self._event_logger

        self._vent_ctrl = VentController(self._ha, event_logger=self._event_logger)

        await self._sync_engines()

        # Subscribe to ALL HA state changes
        self._ha.subscribe_all(self._on_state_change)

        self._apscheduler.add_job(
            self._tick_all,
            "interval",
            seconds=60,
            id="main_tick",
            max_instances=1,
            coalesce=True,
        )
        self._apscheduler.add_job(
            self._purge_old_logs,
            "interval",
            hours=24,
            id="log_purge",
            max_instances=1,
            coalesce=True,
        )
        # Daily metrics rollup at 00:05 local — recomputes yesterday + today so
        # cycles that crossed midnight or arrived late are picked up. (#85 1d)
        self._apscheduler.add_job(
            self._rollup_daily_metrics_job,
            "cron",
            hour=0,
            minute=5,
            id="daily_metrics_rollup",
            max_instances=1,
            coalesce=True,
        )
        # Monthly metrics rollup at 00:10 local on the 1st — recomputes the
        # previous month and the current month-to-date. (#85 1e)
        self._apscheduler.add_job(
            self._rollup_monthly_metrics_job,
            "cron",
            day=1,
            hour=0,
            minute=10,
            id="monthly_metrics_rollup",
            max_instances=1,
            coalesce=True,
        )
        self._apscheduler.start()

        # Run purge immediately on startup to clean up before the first tick.
        await self._purge_old_logs()

        log.info("Scheduler started (system_enabled=%s)", self._system_enabled)

    async def stop(self) -> None:
        self._apscheduler.shutdown(wait=False)
        # Cancel any still-pending background tasks before closing the DB they
        # may be using (Issue #304).
        for task in list(self._bg_tasks):
            task.cancel()
        if self._db_conn:
            await self._db_conn.close()
        log.info("Scheduler stopped")

    async def reload_db(self) -> None:
        """Close and reopen the DB connection (e.g. after a restore), then
        re-sync engines from the new data. APScheduler keeps running."""
        if self._db_conn:
            await self._db_conn.close()
        self._db_conn = await aiosqlite.connect(self._db_path)
        self._db_conn.row_factory = aiosqlite.Row
        await db.init_db(self._db_conn)
        if self._event_logger:
            self._event_logger.set_conn(self._db_conn)
        val = await db.get_system_setting(self._db_conn, "system_enabled", "1")
        self._system_enabled = val == "1"
        dev_val = await db.get_system_setting(self._db_conn, "developer_mode", "0")
        self._dev_mode = dev_val == "1"
        vac_val = await db.get_system_setting(self._db_conn, "vacation_mode_enabled", "0")
        self._vacation_mode = vac_val == "1"
        vac_return = await db.get_system_setting(self._db_conn, "vacation_mode_return_at", "")
        self._vacation_return_at = datetime.fromisoformat(vac_return) if vac_return else None
        if self._unit_override not in ("F", "C"):
            self._active_unit = await db.get_system_setting(self._db_conn, "temperature_unit", "F")
        self._ha.dev_mode = self._dev_mode
        self._ha._dev_logger = self._event_logger
        await self._sync_engines()
        log.info(
            "Scheduler DB reloaded (system_enabled=%s, dev_mode=%s)",
            self._system_enabled,
            self._dev_mode,
        )
        if self._event_logger:
            await self._event_logger.log("info", "system", "Database restored and reloaded")

    async def get_db(self) -> aiosqlite.Connection:
        return self._db_conn

    # ------------------------------------------------------------------
    # System enable / disable
    # ------------------------------------------------------------------

    def get_system_enabled(self) -> bool:
        return self._system_enabled

    def get_engine(self, thermostat_entity_id: str) -> CycleEngine | None:
        """Return the engine for a thermostat, or None if not (yet) created.

        Used by ``/api/thermostat-health`` to read per-zone availability state
        (Issue #267) without reaching into the private engine map.
        """
        return self._engines.get(thermostat_entity_id)

    async def set_system_enabled(self, enabled: bool) -> None:
        self._system_enabled = enabled
        await db.set_system_setting(self._db_conn, "system_enabled", "1" if enabled else "0")
        log.info("System %s", "enabled" if enabled else "disabled")
        await self._reset_and_reevaluate(reason=f"system {'enabled' if enabled else 'disabled'}")
        if self._broadcast:
            await self._broadcast("system_enabled_changed", {"enabled": enabled})

    def get_dev_mode(self) -> bool:
        return self._dev_mode

    async def set_dev_mode(self, enabled: bool) -> None:
        self._dev_mode = enabled
        self._ha.dev_mode = enabled
        await db.set_system_setting(self._db_conn, "developer_mode", "1" if enabled else "0")
        log.info("Developer mode %s", "enabled" if enabled else "disabled")
        if self._event_logger:
            await self._event_logger.log(
                "info",
                "system",
                f"Developer mode {'enabled' if enabled else 'disabled'}",
            )
        await self._reset_and_reevaluate(
            reason=f"developer mode {'enabled' if enabled else 'disabled'}"
        )
        if self._broadcast:
            await self._broadcast("dev_mode_changed", {"dev_mode": enabled})

    # ------------------------------------------------------------------
    # Temperature unit
    # ------------------------------------------------------------------

    def get_temperature_unit(self) -> str:
        """Return the active temperature unit ('F' or 'C') for this session."""
        return self._active_unit

    async def get_unit_change_ack_required(self) -> bool:
        val = await db.get_system_setting(self._db_conn, "unit_change_ack_required", "0")
        return val == "1"

    async def ack_unit_change(self) -> None:
        """Clear the unit-change acknowledgement flag and record which HA unit was
        acknowledged, so the per-tick check does not immediately re-raise the
        banner for the same still-pending change (Issue #288). The mismatch is
        only truly resolved by the restart that applies the new unit."""
        await db.set_system_setting(self._db_conn, "unit_change_ack_required", "0")
        try:
            acked_unit = await self._ha.get_temperature_unit()
        except Exception:
            return
        await db.set_system_setting(self._db_conn, "unit_change_acked_unit", acked_unit)

    async def _clear_unit_change_banner(self) -> None:
        """Reset all unit-change banner bookkeeping (flag + acknowledged unit)."""
        await db.set_system_setting(self._db_conn, "unit_change_ack_required", "0")
        await db.set_system_setting(self._db_conn, "unit_change_acked_unit", "")

    # ------------------------------------------------------------------
    # Vacation mode
    # ------------------------------------------------------------------

    def get_vacation_mode(self) -> bool:
        return self._vacation_mode

    def get_vacation_return_at(self) -> datetime | None:
        return self._vacation_return_at

    async def set_vacation_mode(self, enabled: bool, return_at: datetime | None = None) -> None:
        self._vacation_mode = enabled
        self._vacation_return_at = return_at if enabled else None
        await db.set_system_setting(self._db_conn, "vacation_mode_enabled", "1" if enabled else "0")
        await db.set_system_setting(
            self._db_conn,
            "vacation_mode_return_at",
            return_at.isoformat() if (enabled and return_at) else "",
        )
        log.info("Vacation mode %s", "enabled" if enabled else "disabled")
        if self._event_logger:
            await self._event_logger.log(
                "info",
                "system",
                f"Vacation mode {'enabled until ' + return_at.isoformat() if enabled and return_at else 'disabled'}",
            )
        await self._reset_and_reevaluate(
            reason=f"vacation mode {'enabled' if enabled else 'disabled'}"
        )
        if self._broadcast:
            await self._broadcast(
                "vacation_mode_changed",
                {
                    "enabled": enabled,
                    "return_at": return_at.isoformat() if (enabled and return_at) else None,
                },
            )

    async def _check_vacation_expiry(self) -> None:
        if not self._vacation_mode:
            return
        if self._vacation_return_at and datetime.now(UTC) >= self._vacation_return_at:
            log.info("Vacation mode expired — resuming normal scheduling")
            await self.set_vacation_mode(False)

    async def _startup_resolve_unit(self) -> None:
        """Background task: wait for HA to connect, then persist the detected unit."""
        try:
            await self._ha.wait_connected(timeout=60)
        except TimeoutError:
            return
        try:
            unit = await self._ha.get_temperature_unit()
        except Exception as exc:
            log.warning("Could not resolve temperature unit from HA: %s", exc)
            return
        self._active_unit = unit
        await db.set_system_setting(self._db_conn, "temperature_unit", unit)
        # The stored unit now matches HA, so any pending unit-change banner is
        # resolved by this (re)start — clear it even if the user never dismissed
        # it before restarting (Issue #288).
        await self._clear_unit_change_banner()
        log.info("Temperature unit resolved from HA on startup: %s", unit)

    async def _check_unit_change(self) -> None:
        """Called on each tick: detect HA unit changes and set the ack flag.

        The stored ``temperature_unit`` is only rewritten at startup (the running
        session's active unit is locked then), so a live HA unit change stays
        mismatched until the user restarts. To keep the dismiss meaningful we
        record the acknowledged HA unit and don't re-raise the banner for it; we
        re-flag only when HA moves to a unit that has not been acknowledged. Once
        the mismatch resolves (HA matches stored — e.g. after the applying
        restart) the banner bookkeeping is cleared. (Issue #288)
        """
        if not self._ha._connected.is_set():
            return
        if self._unit_override in ("F", "C"):
            return
        try:
            ha_unit = await self._ha.get_temperature_unit()
        except Exception:
            return
        stored = await db.get_system_setting(self._db_conn, "temperature_unit", "F")
        if ha_unit == stored:
            # No pending change (or it has been applied) — clear stale banner state.
            await self._clear_unit_change_banner()
            return
        acked = await db.get_system_setting(self._db_conn, "unit_change_acked_unit", "")
        if ha_unit != acked:
            await db.set_system_setting(self._db_conn, "unit_change_ack_required", "1")
            log.info(
                "Temperature unit change detected: stored=%s HA=%s — ack required",
                stored,
                ha_unit,
            )

    async def _reset_and_reevaluate(self, reason: str) -> None:
        """Terminate every open cycle, then tick every engine so they re-evaluate
        what should be running under the new system/dev mode state.

        Invoked on every system/dev toggle (on or off). Guarantees that cycles
        from a previous mode never linger as ACTIVE after a transition and that
        engine state tracks the flag change without waiting for the next
        scheduled 60s tick.
        """
        if not self._engines:
            return
        # 1. Force-abort any in-flight cycle in each engine — regardless of the
        # current enabled flag. The normal tick-driven abort only fires when
        # _get_enabled() is False, so transitions like system-on or
        # dev-off-while-system-on wouldn't otherwise clean up.
        for tid, eng in self._engines.items():
            try:
                await eng.force_abort(self._db_conn, reason=reason)
            except Exception as exc:
                log.error("force_abort failed for %s (%s): %s", tid, reason, exc)
        # 2. Safety net: close any cycle_log rows that are still open despite
        # the force_abort above. Catches edge cases where DB close inside
        # _abort_cycle failed, so the UI never shows stale "Active" cycles.
        for tid in self._engines:
            try:
                closed = await db.close_open_cycle_logs(self._db_conn, tid)
                if closed:
                    log.warning(
                        "Safety-net: closed %d orphaned cycle log(s) for %s after %s",
                        closed,
                        tid,
                        reason,
                    )
            except Exception as exc:
                log.error("Safety-net cycle cleanup failed for %s: %s", tid, exc)
        # 3. Re-evaluate: tick every engine so they start a fresh cycle if
        # still needed under the new flag state. Engines that are now disabled
        # will see that and skip; engines newly enabled will pick up work.
        tasks = [self._tick_engine(tid, eng) for tid, eng in self._engines.items()]
        await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # Engine management
    # ------------------------------------------------------------------

    async def refresh_engines(self) -> None:
        await self._sync_engines()

    async def _sync_engines(self) -> None:
        rooms = await db.get_all_rooms(self._db_conn)
        thermostat_ids = {r.thermostat_entity_id for r in rooms}

        assert self._vent_ctrl is not None
        for tid in thermostat_ids:
            if tid not in self._engines:
                engine = CycleEngine(
                    thermostat_entity_id=tid,
                    ha=self._ha,
                    vent_ctrl=self._vent_ctrl,
                    broadcast=self._broadcast,
                    event_logger=self._event_logger,
                    # Engine runs when system is enabled OR dev mode is on
                    get_enabled=lambda: self._system_enabled or self._dev_mode,
                    get_vacation_mode=lambda: self._vacation_mode,
                )
                self._engines[tid] = engine
                log.info("CycleEngine created for %s", tid)
                # Restore any in-progress cycle state from DB so the engine
                # doesn't start cold after a server restart.
                await engine.restore_from_db(self._db_conn)

        for tid in list(self._engines):
            if tid not in thermostat_ids:
                # The thermostat's last room was removed. Abort any in-flight
                # cycle first — otherwise the HA thermostat keeps the engine's
                # overshoot setpoint (HVAC runs on), vents stay in their cycle
                # positions, and the open cycle_log row is never closed, showing
                # a permanently "Active" cycle. The _reset_and_reevaluate orphan
                # safety net can't catch this engine because it only scans
                # engines still in the map. Abort + close before del. (#285)
                eng = self._engines[tid]
                try:
                    await eng.force_abort(self._db_conn, reason="thermostat removed")
                except Exception as exc:
                    log.error("force_abort failed while removing engine %s: %s", tid, exc)
                try:
                    closed = await db.close_open_cycle_logs(self._db_conn, tid)
                    if closed:
                        log.warning(
                            "Closed %d orphaned cycle log(s) for removed thermostat %s",
                            closed,
                            tid,
                        )
                except Exception as exc:
                    log.error("Cycle-log cleanup failed while removing engine %s: %s", tid, exc)
                del self._engines[tid]
                log.info("CycleEngine removed for %s", tid)

    # ------------------------------------------------------------------
    # Log purge
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Metrics rollup (Issue #85 Phase 1d/1e)
    # ------------------------------------------------------------------

    async def _rollup_daily_metrics_job(self) -> None:
        """APScheduler entry — recompute the local-date window [yesterday, today]."""
        await self.run_daily_metrics_rollup()

    async def _rollup_monthly_metrics_job(self) -> None:
        """APScheduler entry — recompute the [previous month, current month] window."""
        await self.run_monthly_metrics_rollup()

    async def run_daily_metrics_rollup(self, days_back: int = 1) -> int:
        """Recompute the daily metrics rollup for the last `days_back` local
        days plus today. Also exposed via the manual-trigger API endpoint
        (Issue #85 Phase 1d) so tests and operators can force a recompute.

        Returns the number of (date, thermostat) rows produced.
        """
        today_local = tz.today_local()
        start = today_local - timedelta(days=max(0, days_back))
        n = await db.rollup_daily_metrics(self._db_conn, start.isoformat(), today_local.isoformat())
        log.info(
            "Daily metrics rollup complete — %d row(s) for %s..%s",
            n,
            start.isoformat(),
            today_local.isoformat(),
        )
        return n

    async def run_monthly_metrics_rollup(self, months_back: int = 1) -> int:
        """Recompute monthly metrics for the last `months_back` whole months
        plus the current month-to-date. (#85 Phase 1e)

        Returns the number of (month, thermostat) rows produced.
        """
        today_local = tz.today_local()
        # Compute "month N back" by walking to the first of this month then back N months.
        first_of_this_month = today_local.replace(day=1)
        cur = first_of_this_month
        for _ in range(max(0, months_back)):
            cur = (cur - timedelta(days=1)).replace(day=1)
        start_month = cur.strftime("%Y-%m")
        end_month = today_local.strftime("%Y-%m")
        n = await db.rollup_monthly_metrics(self._db_conn, start_month, end_month)
        log.info(
            "Monthly metrics rollup complete — %d row(s) for %s..%s",
            n,
            start_month,
            end_month,
        )
        return n

    async def _purge_old_logs(self) -> None:
        """Delete event and cycle logs older than their configured retention periods."""
        event_days = int(
            await db.get_system_setting(self._db_conn, "event_log_retention_days", "7")
        )
        cycle_days = int(
            await db.get_system_setting(self._db_conn, "cycle_log_retention_days", "30")
        )
        ev_count = await db.purge_event_logs(self._db_conn, event_days)
        cy_count = await db.purge_cycle_logs(self._db_conn, cycle_days)
        if ev_count or cy_count:
            log.info(
                "Log purge complete — removed %d event rows (>%dd), %d cycle rows (>%dd)",
                ev_count,
                event_days,
                cy_count,
                cycle_days,
            )

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    async def _tick_all(self) -> None:
        await self._check_vacation_expiry()
        await self._check_unit_change()
        await self._sync_engines()
        await self._refresh_continuous_presence()
        tasks = [self._tick_engine(tid, eng) for tid, eng in self._engines.items()]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _refresh_continuous_presence(self) -> None:
        """Refresh presence holdovers for rooms whose presence sensor currently
        reads "on".

        Presence is otherwise edge-triggered (``_on_state_change`` only fires on
        a transition to "on"). Many occupancy sensors (mmWave, some PIR
        aggregations) hold a single continuous "on" state while a room is
        occupied and emit no further state_changed events, so without this the
        holdover would expire mid-occupancy and the room would deactivate even
        though it is still occupied (Issue #287). Runs before the engine ticks so
        the refreshed holdover is visible when each tick resolves active rooms.
        """
        rooms = await db.get_all_rooms(self._db_conn)
        for room in rooms:
            if room.presence_holdover_hours <= 0:
                continue
            engine = self._engines.get(room.thermostat_entity_id)
            if engine is None:
                continue
            presence_sensors = await db.get_room_presence_sensors(self._db_conn, room.id)
            for ps in presence_sensors:
                state = self._ha.get_state(ps.entity_id)
                if state and state.get("state") == "on":
                    await engine.handle_presence(self._db_conn, room)
                    break

    async def _tick_engine(self, tid: str, engine: CycleEngine) -> None:
        # The whole body — the pre-tick DB reads AND the tick — runs under the
        # retry loop. Previously the reads sat outside the try/except, so a
        # transient failure there propagated into _tick_all's
        # gather(return_exceptions=True) and was discarded with zero log output,
        # silently leaving the zone uncontrolled. Retry a few times (transient
        # locks clear in milliseconds) before giving up and surfacing the error
        # to container logs and the UI Live Feed. (Issue #286)
        for attempt in range(1, _TICK_MAX_ATTEMPTS + 1):
            try:
                rooms = await db.get_rooms_for_thermostat(self._db_conn, tid)
                await engine.load_room_sensors(self._db_conn, [r.id for r in rooms])
                await engine.tick(self._db_conn)
                return
            except Exception as exc:
                if attempt < _TICK_MAX_ATTEMPTS:
                    log.warning(
                        "Tick attempt %d/%d failed for %s: %s — retrying",
                        attempt,
                        _TICK_MAX_ATTEMPTS,
                        tid,
                        exc,
                    )
                    await asyncio.sleep(_TICK_RETRY_BACKOFF_S * attempt)
                    continue
                # Retries exhausted — log loudly and mirror to the event logger
                # so the UI Live Feed surfaces tick crashes (vent service errors,
                # HA unavailability, DB locks, anything else). Without this,
                # errors only land in container logs and the user has no way to
                # notice from inside the app.
                log.exception(
                    "Tick failed for %s after %d attempts: %s",
                    tid,
                    _TICK_MAX_ATTEMPTS,
                    exc,
                )
                if self._event_logger:
                    try:
                        await self._event_logger.log(
                            "error",
                            "engine",
                            f"Tick failed for {tid} after {_TICK_MAX_ATTEMPTS} attempts: {exc}",
                            {"thermostat": tid, "error": str(exc), "attempts": _TICK_MAX_ATTEMPTS},
                        )
                    except Exception:
                        log.exception("Failed to write tick error to event logger for %s", tid)

    # ------------------------------------------------------------------
    # HA state change dispatch
    # ------------------------------------------------------------------

    async def _on_state_change(self, entity_id: str, new_state: dict) -> None:
        if entity_id.startswith("binary_sensor.") and new_state.get("state") == "on":
            await self._handle_presence_event(entity_id)

        if entity_id.startswith("climate.") and entity_id in self._engines:
            await self._tick_engine(entity_id, self._engines[entity_id])

    async def _handle_presence_event(self, presence_entity_id: str) -> None:
        rooms = await db.get_all_rooms(self._db_conn)
        for room in rooms:
            presence_sensors = await db.get_room_presence_sensors(self._db_conn, room.id)
            for ps in presence_sensors:
                if ps.entity_id == presence_entity_id:
                    if self._event_logger:
                        await self._event_logger.log(
                            "info",
                            "presence",
                            f"Presence detected in {room.name} via {presence_entity_id}",
                            {"room_id": room.id, "sensor": presence_entity_id},
                        )
                    engine = self._engines.get(room.thermostat_entity_id)
                    if engine:
                        await engine.handle_presence(self._db_conn, room)
                        await self._tick_engine(room.thermostat_entity_id, engine)

    # ------------------------------------------------------------------
    # Status helpers for API
    # ------------------------------------------------------------------

    def get_all_zone_statuses(self) -> list[dict]:
        return [
            {
                "thermostat_entity_id": tid,
                "cycle_state": engine.cycle_state.value,
                **self._zone_status_dict(engine),
            }
            for tid, engine in self._engines.items()
        ]

    def _zone_status_dict(self, engine: CycleEngine) -> dict:
        s = engine.get_zone_status()
        return {
            "hvac_mode": s.hvac_mode,
            "hvac_action": s.hvac_action,
            "current_temp": s.current_temp,
            "setpoint": s.setpoint,
            "cycle_id": s.cycle_id,
            "cycle_started_at": s.cycle_started_at.isoformat() if s.cycle_started_at else None,
            "rooms": [
                {
                    "room_id": r.room_id,
                    "avg_temp": r.avg_temp,
                    "vent_states": r.vent_states,
                    "presence_active": r.presence_active,
                }
                for r in s.rooms
            ],
        }
