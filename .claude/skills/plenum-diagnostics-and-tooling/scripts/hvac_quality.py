#!/usr/bin/env python3
"""hvac_quality.py — HVAC-quality red-flag scan of a Plenum app.db (read-only).

Usage:
    python3 hvac_quality.py /path/to/app.db [--days 7] [--thermostat climate.x]
                            [--short-min 10] [--offtime-min 10]

Scans completed cycle_logs / room_cycle_states for four failure signatures:

1. SHORT CYCLES     — runtime < --short-min minutes (default 10). Compressor
                      killer; if frequent, raise min_cycle_runtime_min (#208).
2. SHORT OFF-TIMES  — gap between one cycle's end and the next cycle's start
                      < --offtime-min minutes on the same thermostat. If
                      frequent, raise min_cycle_offtime_min (#208).
3. MODE OSCILLATION — heating cycle followed by a cooling cycle (or vice
                      versa) within 60 min on the same thermostat. Points at
                      fighting setpoints / rooms with opposing targets.
4. DRIFTING ROOMS   — per-room avg signed miss (temp_at_end - target_temp,
                      sign-normalised so + = wrong side / overshoot in mode
                      direction) and rooms that rarely reach target
                      (reached_at NULL rate).

All temperatures are storage °F; deltas are °F deltas (no -32, see #291).
Exit code 1 if any red flag fired (usable in cron/CI health checks).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--thermostat", default=None)
    ap.add_argument("--short-min", type=float, default=10.0)
    ap.add_argument("--offtime-min", type=float, default=10.0)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    since = (datetime.utcnow() - timedelta(days=args.days)).isoformat()

    where = "ended_at IS NOT NULL AND started_at >= ?"
    params: list = [since]
    if args.thermostat:
        where += " AND thermostat_entity_id = ?"
        params.append(args.thermostat)

    cycles = conn.execute(
        f"""SELECT id, thermostat_entity_id, mode, started_at, ended_at, ended_reason,
                   (julianday(ended_at) - julianday(started_at)) * 1440.0 AS dur_min
            FROM cycle_logs WHERE {where} ORDER BY thermostat_entity_id, started_at""",
        params,
    ).fetchall()
    if not cycles:
        print(f"No completed cycles in the last {args.days} day(s).")
        return 0

    flags = 0

    # 1. Short cycles
    short = [c for c in cycles if c["dur_min"] < args.short_min]
    print(f"[1] short cycles (<{args.short_min:g} min): {len(short)}/{len(cycles)} "
          f"({100.0 * len(short) / len(cycles):.0f}%)")
    for c in short[:10]:
        print(f"      {c['id']} {c['mode']:<8} {c['started_at'][:19]} "
              f"{c['dur_min']:.1f} min  ({c['ended_reason']})")
    if len(short) > 10:
        print(f"      ... and {len(short) - 10} more")
    if len(short) / len(cycles) > 0.2:
        print("      RED FLAG: >20% short cycles — check min_cycle_runtime_min / deadband")
        flags += 1

    # 2. Short off-times + 3. Mode oscillation (walk per-thermostat in time order)
    short_off, oscill = [], []
    prev = None
    for c in cycles:
        if prev and prev["thermostat_entity_id"] == c["thermostat_entity_id"]:
            gap_min = (datetime.fromisoformat(c["started_at"])
                       - datetime.fromisoformat(prev["ended_at"])).total_seconds() / 60.0
            if gap_min < args.offtime_min:
                short_off.append((prev, c, gap_min))
            if prev["mode"] != c["mode"] and gap_min < 60:
                oscill.append((prev, c, gap_min))
        prev = c

    print(f"\n[2] short off-times (<{args.offtime_min:g} min between cycles): {len(short_off)}")
    for p, c, g in short_off[:10]:
        print(f"      {p['id']} -> {c['id']}  gap {g:.1f} min  ({p['mode']} -> {c['mode']})")
    if len(short_off) > len(cycles) * 0.2:
        print("      RED FLAG: frequent restarts — check min_cycle_offtime_min / overshoot_delta")
        flags += 1

    print(f"\n[3] heat<->cool oscillations (mode flip within 60 min): {len(oscill)}")
    for p, c, g in oscill[:10]:
        print(f"      {p['id']}({p['mode']}) -> {c['id']}({c['mode']})  gap {g:.1f} min")
    if oscill:
        print("      RED FLAG: opposite-cycle fighting — check room targets / deadbands")
        flags += 1

    # 4. Drifting rooms / never-reach rooms
    print("\n[4] per-room target performance:")
    where_cl = where.replace("ended_at", "cl.ended_at").replace("started_at", "cl.started_at") \
                    .replace("thermostat_entity_id", "cl.thermostat_entity_id")
    rrows = conn.execute(
        f"""SELECT rcs.room_id, r.name,
                   COUNT(*) AS n,
                   SUM(CASE WHEN rcs.reached_at IS NULL THEN 1 ELSE 0 END) AS never_reached,
                   AVG(CASE WHEN cl.mode = 'heating' THEN rcs.temp_at_end - rcs.target_temp
                            WHEN cl.mode = 'cooling' THEN rcs.target_temp - rcs.temp_at_end
                       END) AS avg_signed_miss
            FROM room_cycle_states rcs
            JOIN cycle_logs cl ON cl.id = rcs.cycle_id
            LEFT JOIN rooms r ON r.id = rcs.room_id
            WHERE rcs.role != 'overflow' AND rcs.temp_at_end IS NOT NULL AND {where_cl}
            GROUP BY rcs.room_id ORDER BY r.name""",
        params,
    ).fetchall()
    for r in rrows:
        miss = r["avg_signed_miss"]
        nr_pct = 100.0 * r["never_reached"] / r["n"]
        note = ""
        if miss is not None and miss > 2.0:
            note = "  <-- chronic overshoot (avg >2°F past target)"
            flags += 1
        elif miss is not None and miss < -2.0:
            note = "  <-- chronic undershoot / drifting (avg >2°F short of target)"
            flags += 1
        if nr_pct > 50:
            note += "  <-- reaches target in <50% of cycles"
            flags += 1
        print(f"      {r['name'] or r['room_id']:<16} n={r['n']:<4} "
              f"never_reached={nr_pct:.0f}%  "
              f"avg_miss={'+' if miss and miss > 0 else ''}{miss:.2f}°F{note}"
              if miss is not None else
              f"      {r['name'] or r['room_id']:<16} n={r['n']:<4} (no end temps)")

    print(f"\n{flags} red flag(s).")
    return 1 if flags else 0


if __name__ == "__main__":
    try:
        import signal
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # behave well under `| head`
    except (ImportError, AttributeError, ValueError):
        pass
    sys.exit(main())
