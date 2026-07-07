"""Deterministic demo metrics data for the Metrics page (Issue #442).

Seeds a fixed, formula-generated week of completed cycles — cycle logs,
room-cycle states (including Eco Mode relaxation), temperature samples, and
vent events — for every registered thermostat, into a fixed past date window.

Two consumers:

1. **E2E visual regression.** The golden-screenshot suite needs the Metrics
   charts to render *identical pixels* on every run, so the dataset must be a
   pure function of (start_date, registered thermostats/rooms). There is
   deliberately no RNG and no clock in here — reseeding with the same inputs
   produces byte-identical rows, which is what lets the update pass and the
   verify pass of the screenshot workflow agree.

2. **Local development / demos.** A fresh install has an empty Metrics page
   until the engine has run for days; seeding gives every chart realistic
   shape immediately (via the DevMode page button or
   ``POST /api/dev/seed-demo-metrics``).

All temperatures are stored °F, like every other row in the database — the
frontend converts for display, so the same seed serves both unit modes.

Reseeding first deletes previously seeded rows (cycle ids carry the
``demo-`` prefix; child tables cascade), so the call is idempotent and never
touches real engine-written history.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta

import aiosqlite

from . import db

log = logging.getLogger(__name__)

# Cycle-id prefix marking seeded rows — the delete side of the reseed and the
# "is this demo data?" discriminator for anyone querying the DB by hand.
DEMO_CYCLE_PREFIX = "demo-"

# Default window. A fixed *past* week so live engine cycles (dated "now")
# never land inside it — the E2E suite pins the Metrics page to this exact
# range under CI (see frontend/src/ci.tsx CI_METRICS_RANGE).
DEFAULT_START_DATE = "2025-06-01"
DEFAULT_DAYS = 7

# Requested (pre-Eco) targets, °F.
_HEATING_TARGET_F = 68.0
_COOLING_TARGET_F = 71.0

# Mirror of the Eco Mode cooling ramp defaults (docs/eco-mode.md): relax
# starts at 86 °F outdoors, full 4 °F drift at 100 °F.
_ECO_THRESHOLD_F = 86.0
_ECO_FULL_DRIFT_TEMP_F = 100.0
_ECO_MAX_DRIFT_F = 4.0


def _eco_drift_f(outside_f: float) -> float:
    """Cooling drift the seeded 'engine' applied at this outdoor temp (°F)."""
    if outside_f < _ECO_THRESHOLD_F:
        return 0.0
    f = min(1.0, (outside_f - _ECO_THRESHOLD_F) / (_ECO_FULL_DRIFT_TEMP_F - _ECO_THRESHOLD_F))
    return round(f * _ECO_MAX_DRIFT_F, 2)


async def seed_demo_metrics(
    conn: aiosqlite.Connection,
    start_date: str = DEFAULT_START_DATE,
    days: int = DEFAULT_DAYS,
) -> dict:
    """(Re)seed the demo metrics dataset. Returns a summary dict.

    Deterministic: same (start_date, days, thermostats, rooms) → same rows.
    """
    thermostats = await db.get_all_thermostat_configs(conn)
    thermostats.sort(key=lambda tc: tc.thermostat_entity_id)

    # Wipe any previous demo rows first (idempotent reseed). Child tables
    # reference cycle_logs with ON DELETE CASCADE.
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("DELETE FROM cycle_logs WHERE id LIKE ?", (f"{DEMO_CYCLE_PREFIX}%",))

    start = date.fromisoformat(start_date)
    seeded_cycles = 0
    eco_cycles = 0

    for ti, tc in enumerate(thermostats):
        rooms = await db.get_rooms_for_thermostat(conn, tc.thermostat_entity_id)
        rooms.sort(key=lambda r: r.name)
        if not rooms:
            continue

        for d in range(days):
            day = start + timedelta(days=d)
            # Outdoor temperature climbs through the week (58 → 88 °F base),
            # giving heating days early, cooling days late, and Eco-eligible
            # (>86 °F) afternoons on the last two days.
            day_base_out = 58.0 + 5.0 * d
            n_cycles = 3 + ((d + ti) % 3)

            for c in range(n_cycles):
                outside = day_base_out + 2.0 * c
                mode = "heating" if day_base_out < 65.0 else "cooling"
                requested = _HEATING_TARGET_F if mode == "heating" else _COOLING_TARGET_F
                drift = _eco_drift_f(outside) if mode == "cooling" else 0.0
                eco_active = drift > 0.0
                effective = requested + drift  # cooling relaxes warmer

                started = datetime.combine(day, time(hour=6 + 3 * c, minute=7 * ti + 4 * c))
                # One deliberately short (<10 min) cycle every other day feeds
                # the short-cycles chart; the rest run 14–35 minutes.
                duration_min = 6 if c == n_cycles - 1 and d % 2 == 0 else 14 + 7 * ((c + d) % 4)
                ended = started + timedelta(minutes=duration_min)

                if d == 3 and c == 0:
                    reason = "aborted: timeout"
                elif d == 5 and c == 0:
                    reason = "aborted: mode change"
                else:
                    reason = "completed"

                sign = 1.0 if mode == "cooling" else -1.0
                temp_start = requested + sign * 2.5
                temp_end = effective - sign * 0.4

                participants = [
                    (r_idx, room)
                    for r_idx, room in enumerate(rooms)
                    if r_idx == 0 or (c + d + r_idx) % 2 == 0
                ]

                cycle_id = f"{DEMO_CYCLE_PREFIX}{tc.thermostat_entity_id}-{day.isoformat()}-{c}"
                rooms_json = {
                    room.id: {
                        "name": room.name,
                        "target": effective,
                        "source": (
                            "override"
                            if r_idx == 0 and c % 5 == 4
                            else "schedule"
                            if r_idx % 2 == 0
                            else "presence"
                        ),
                    }
                    for r_idx, room in participants
                }

                await conn.execute(
                    """INSERT INTO cycle_logs (
                           id, thermostat_entity_id, started_at, ended_at, mode,
                           rooms_json, ended_reason,
                           thermostat_temp_at_start, thermostat_temp_at_end,
                           setpoint_at_start, setpoint_at_end,
                           outside_temp_at_start, outside_temp_at_end)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        cycle_id,
                        tc.thermostat_entity_id,
                        started.isoformat(),
                        ended.isoformat(),
                        mode,
                        json.dumps(rooms_json),
                        reason,
                        temp_start,
                        temp_end,
                        effective,
                        effective,
                        outside,
                        outside + 1.0,
                    ),
                )
                seeded_cycles += 1
                if eco_active:
                    eco_cycles += 1

                reached = (
                    started + timedelta(minutes=duration_min * 0.6)
                    if reason == "completed"
                    else None
                )

                for r_idx, room in participants:
                    room_eco = eco_active and r_idx <= 1
                    await conn.execute(
                        """INSERT INTO room_cycle_states (
                               cycle_id, room_id, target_temp, reached_at,
                               vent_closed_at, temp_at_start, temp_at_end,
                               role, requested_target, effective_target, eco_active)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            cycle_id,
                            room.id,
                            effective,
                            reached.isoformat() if reached else None,
                            reached.isoformat() if reached else None,
                            requested + sign * (3.0 + 0.5 * r_idx),
                            effective - sign * 0.5,
                            "active",
                            requested if room_eco else effective,
                            effective,
                            1 if room_eco else 0,
                        ),
                    )

                    # Vent events: open at cycle start, close when the room
                    # reached target (matching the engine's action strings).
                    vents = await db.get_room_vents(conn, room.id)
                    for vent in sorted(vents, key=lambda v: v.entity_id):
                        await conn.execute(
                            """INSERT INTO cycle_vent_events
                                   (cycle_id, timestamp, entity_id, room_id, action, reason)
                               VALUES (?,?,?,?,?,?)""",
                            (
                                cycle_id,
                                started.isoformat(),
                                vent.entity_id,
                                room.id,
                                "opened_at_start",
                                None,
                            ),
                        )
                        if reached is not None:
                            await conn.execute(
                                """INSERT INTO cycle_vent_events
                                       (cycle_id, timestamp, entity_id, room_id, action, reason)
                                   VALUES (?,?,?,?,?,?)""",
                                (
                                    cycle_id,
                                    reached.isoformat(),
                                    vent.entity_id,
                                    room.id,
                                    "closed_reached_target",
                                    None,
                                ),
                            )

                # Temperature samples every ~5 minutes: the thermostat walks
                # linearly from temp_start to temp_end; each room walks toward
                # its own extreme so the overshoot histogram gets a spread of
                # 0 / 0.7 / 1.4 °F bins.
                n_samples = max(2, duration_min // 5 + 1)
                for s in range(n_samples):
                    frac = s / (n_samples - 1)
                    ts = started + timedelta(minutes=duration_min * frac)
                    thermo_temp = round(temp_start + (temp_end - temp_start) * frac, 2)
                    for r_idx, room in participants:
                        overshoot = ((c + d + r_idx) % 3) * 0.7
                        room_start = requested + sign * (3.0 + 0.5 * r_idx)
                        room_end = effective - sign * overshoot
                        room_temp = round(room_start + (room_end - room_start) * frac, 2)
                        await conn.execute(
                            """INSERT INTO cycle_temp_samples
                                   (cycle_id, room_id, timestamp, room_temp,
                                    thermostat_temp, setpoint)
                               VALUES (?,?,?,?,?,?)""",
                            (
                                cycle_id,
                                room.id,
                                ts.isoformat(),
                                room_temp,
                                thermo_temp,
                                effective,
                            ),
                        )

    await conn.commit()
    end = start + timedelta(days=days - 1)
    log.info(
        "Seeded %d demo cycles (%d eco-active) for %d thermostats over %s → %s",
        seeded_cycles,
        eco_cycles,
        len(thermostats),
        start_date,
        end.isoformat(),
    )
    return {
        "seeded_cycles": seeded_cycles,
        "eco_cycles": eco_cycles,
        "thermostats": len(thermostats),
        "start_date": start_date,
        "end_date": end.isoformat(),
    }
