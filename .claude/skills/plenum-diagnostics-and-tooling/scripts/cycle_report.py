#!/usr/bin/env python3
"""cycle_report.py — recent-cycle report from a Plenum app.db (read-only).

Usage:
    python3 cycle_report.py /path/to/app.db [--days 7] [--thermostat climate.x] [--limit 50]

Prints one line per cycle (newest first) plus a summary block:
duty cycle %, completion-rate buckets (completed / timeout / aborted, using the
same LIKE-bucketing as db.py's _ROLLUP_REASON_BUCKETS), avg duration, and
per-room reached/closed timing for the most recent completed cycle.

All temperatures printed are raw storage values = °F (Plenum stores °F only).
Timestamps in the DB are naive UTC ISO strings.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta


def connect_ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fmt_t(v) -> str:
    return "-" if v is None else f"{float(v):.1f}"


def fmt_dur(seconds: float | None) -> str:
    if seconds is None:
        return "     -"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}h{m:02d}m" if h else f"{m:d}m{s:02d}s"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--thermostat", default=None)
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    conn = connect_ro(args.db)
    since = (datetime.utcnow() - timedelta(days=args.days)).isoformat()

    where = "started_at >= ?"
    params: list = [since]
    if args.thermostat:
        where += " AND thermostat_entity_id = ?"
        params.append(args.thermostat)

    rows = conn.execute(
        f"""SELECT id, thermostat_entity_id, mode, started_at, ended_at, ended_reason,
                   setpoint_at_start, setpoint_at_end,
                   thermostat_temp_at_start, thermostat_temp_at_end,
                   outside_temp_at_start,
                   (julianday(ended_at) - julianday(started_at)) * 86400.0 AS dur_s
            FROM cycle_logs WHERE {where}
            ORDER BY started_at DESC LIMIT ?""",
        params + [args.limit],
    ).fetchall()

    if not rows:
        print(f"No cycles in the last {args.days} day(s).")
        return 0

    print(f"{'cycle_id':<12} {'mode':<8} {'started (UTC)':<20} {'dur':>7} "
          f"{'sp°F':>6} {'t_start':>7} {'t_end':>6} {'out°F':>6}  ended_reason")
    for r in rows:
        running = r["ended_at"] is None
        print(f"{r['id']:<12} {r['mode']:<8} {r['started_at'][:19]:<20} "
              f"{'RUN' if running else fmt_dur(r['dur_s']):>7} "
              f"{fmt_t(r['setpoint_at_start']):>6} "
              f"{fmt_t(r['thermostat_temp_at_start']):>7} "
              f"{fmt_t(r['thermostat_temp_at_end']):>6} "
              f"{fmt_t(r['outside_temp_at_start']):>6}  "
              f"{r['ended_reason'] or ('(running)' if running else '')}")

    # Summary — completed cycles only, same reason buckets as db.py
    srow = conn.execute(
        f"""SELECT COUNT(*) AS n,
                   SUM(CASE WHEN ended_reason = 'completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN ended_reason LIKE 'aborted: timeout%' OR ended_reason = 'timeout'
                        THEN 1 ELSE 0 END) AS timeouts,
                   SUM(CASE WHEN ended_reason LIKE 'aborted:%'
                            AND NOT (ended_reason LIKE 'aborted: timeout%') THEN 1 ELSE 0 END) AS aborted,
                   AVG((julianday(ended_at) - julianday(started_at)) * 86400.0) AS avg_dur,
                   SUM((julianday(ended_at) - julianday(started_at)) * 86400.0) AS hvac_s
            FROM cycle_logs WHERE ended_at IS NOT NULL AND {where}""",
        params,
    ).fetchone()
    n = srow["n"] or 0
    range_s = args.days * 86400
    print("\n--- summary (completed cycles, last %d day(s)) ---" % args.days)
    print(f"cycles: {n}   completed: {srow['completed'] or 0}   "
          f"timeout: {srow['timeouts'] or 0}   aborted: {srow['aborted'] or 0}")
    if n:
        print(f"completion rate: {100.0 * (srow['completed'] or 0) / n:.1f}%")
        print(f"avg duration: {fmt_dur(srow['avg_dur'])}   "
              f"duty cycle: {100.0 * (srow['hvac_s'] or 0) / range_s:.1f}% "
              f"(single-thermostat basis)")

    # Per-room timing for the most recent completed cycle
    last = conn.execute(
        f"""SELECT id, started_at FROM cycle_logs
            WHERE ended_at IS NOT NULL AND {where}
            ORDER BY started_at DESC LIMIT 1""",
        params,
    ).fetchone()
    if last:
        print(f"\n--- per-room timing for latest completed cycle {last['id']} ---")
        rrows = conn.execute(
            """SELECT rcs.room_id, r.name, rcs.role, rcs.target_temp,
                      rcs.temp_at_start, rcs.temp_at_end,
                      (julianday(rcs.reached_at) - julianday(COALESCE(rcs.joined_at, ?)))
                          * 86400.0 AS to_target_s,
                      (julianday(rcs.vent_closed_at) - julianday(COALESCE(rcs.joined_at, ?)))
                          * 86400.0 AS to_closed_s
               FROM room_cycle_states rcs LEFT JOIN rooms r ON r.id = rcs.room_id
               WHERE rcs.cycle_id = ?""",
            (last["started_at"], last["started_at"], last["id"]),
        ).fetchall()
        for r in rrows:
            print(f"  {r['name'] or r['room_id']:<16} role={r['role']:<9} "
                  f"target={fmt_t(r['target_temp'])}°F start={fmt_t(r['temp_at_start'])} "
                  f"end={fmt_t(r['temp_at_end'])} "
                  f"reached={fmt_dur(r['to_target_s'])} vent_closed={fmt_dur(r['to_closed_s'])}")
    return 0


if __name__ == "__main__":
    try:
        import signal
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # behave well under `| head`
    except (ImportError, AttributeError, ValueError):
        pass
    sys.exit(main())
