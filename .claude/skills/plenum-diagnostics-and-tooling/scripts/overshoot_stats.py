#!/usr/bin/env python3
"""overshoot_stats.py — per-room overshoot analysis from a Plenum app.db (read-only).

Usage:
    python3 overshoot_stats.py /path/to/app.db [--days 7] [--thermostat climate.x]
                               [--bin-size 1.0] [--max-bins 6]

Replicates the semantics of db.compute_overshoot_histogram (Phase 4l, #290):
overshoot per (cycle, room) participation = worst observed temperature on the
wrong side of the target during the cycle:
    cooling: max(target - min(temp_seen), 0)
    heating: max(max(temp_seen) - target, 0)
Room-level cycle_temp_samples are preferred; thermostat-level samples
(room_id IS NULL) are used only for rooms with no room-level readings.
Overflow-role rooms are excluded.

Overshoot values are DELTAS in °F (no -32 offset — see #291). Also prints a
per-room breakdown the API endpoint does not give you.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--thermostat", default=None)
    ap.add_argument("--bin-size", type=float, default=1.0)
    ap.add_argument("--max-bins", type=int, default=6)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    since = (datetime.utcnow() - timedelta(days=args.days)).isoformat()

    where = "cl.ended_at IS NOT NULL AND cl.started_at >= ?"
    params: list = [since]
    if args.thermostat:
        where += " AND cl.thermostat_entity_id = ?"
        params.append(args.thermostat)

    rows = conn.execute(
        f"""SELECT cl.id AS cycle_id, cl.mode,
                   rcs.room_id, rcs.target_temp,
                   s.room_id AS sample_room_id, s.room_temp, s.thermostat_temp
            FROM cycle_logs cl
            JOIN room_cycle_states rcs ON rcs.cycle_id = cl.id AND rcs.role != 'overflow'
            JOIN cycle_temp_samples s ON s.cycle_id = cl.id
                AND (s.room_id = rcs.room_id OR s.room_id IS NULL)
            WHERE {where}""",
        params,
    ).fetchall()

    # Track room-level and thermostat-level extremes separately per (cycle, room).
    ext: dict[tuple, dict] = {}
    for r in rows:
        key = (r["cycle_id"], r["room_id"])
        e = ext.setdefault(key, {"mode": r["mode"], "target": float(r["target_temp"]),
                                 "rmin": None, "rmax": None, "tmin": None, "tmax": None})
        if r["sample_room_id"] is not None:
            temp = r["room_temp"] if r["room_temp"] is not None else r["thermostat_temp"]
            lo, hi = "rmin", "rmax"
        else:
            temp = r["thermostat_temp"]
            lo, hi = "tmin", "tmax"
        if temp is None:
            continue
        t = float(temp)
        if e[lo] is None or t < e[lo]:
            e[lo] = t
        if e[hi] is None or t > e[hi]:
            e[hi] = t

    per_room: dict[str, list[float]] = defaultdict(list)
    overshoots: list[float] = []
    for (cycle_id, room_id), e in ext.items():
        if e["rmin"] is not None or e["rmax"] is not None:
            mn, mx = e["rmin"], e["rmax"]
        else:
            mn, mx = e["tmin"], e["tmax"]
        if e["mode"] == "cooling" and mn is not None:
            os_ = max(e["target"] - mn, 0.0)
        elif e["mode"] == "heating" and mx is not None:
            os_ = max(mx - e["target"], 0.0)
        else:
            continue
        overshoots.append(os_)
        per_room[room_id].append(os_)

    if not overshoots:
        print("No room participations with samples in range.")
        return 0

    bins = [0] * args.max_bins
    for os_ in overshoots:
        bins[min(int(os_ // args.bin_size), args.max_bins - 1)] += 1

    n = len(overshoots)
    overshot = sum(1 for o in overshoots if o > 0)
    print(f"room-participations: {n}   overshot(>0°F): {overshot} ({100.0*overshot/n:.1f}%)")
    print(f"avg overshoot: {sum(overshoots)/n:.2f}°F   max: {max(overshoots):.2f}°F  "
          f"(deltas — do NOT apply the absolute °F→°C formula, see #291)")
    print("\nhistogram:")
    for i, c in enumerate(bins):
        lo = i * args.bin_size
        label = (f">={lo:g}°F" if i == args.max_bins - 1
                 else f"{lo:g}-{lo + args.bin_size:g}°F")
        bar = "#" * int(40 * c / max(bins)) if max(bins) else ""
        print(f"  {label:>10} {c:>5}  {bar}")

    print("\nper-room (chronic-overshoot check):")
    names = dict(conn.execute("SELECT id, name FROM rooms").fetchall())
    for room_id, vals in sorted(per_room.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
        avg = sum(vals) / len(vals)
        flag = "  <-- chronic (avg > 2°F)" if avg > 2.0 else ""
        print(f"  {names.get(room_id, room_id):<16} n={len(vals):<4} "
              f"avg={avg:.2f}°F max={max(vals):.2f}°F{flag}")
    return 0


if __name__ == "__main__":
    try:
        import signal
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # behave well under `| head`
    except (ImportError, AttributeError, ValueError):
        pass
    sys.exit(main())
