#!/usr/bin/env python3
"""event_tail.py — tail the event_log table of a Plenum app.db (read-only).

Usage:
    python3 event_tail.py /path/to/app.db [-n 30] [--category engine]
                          [--level warning,error] [--follow] [--interval 2]

--follow polls for new rows by id (the DB is opened read-only; safe against a
live add-on because Plenum runs SQLite in WAL mode). Categories seen in code
(v0.22.1): system, api, engine, presence, ha, dev, reconcile.
Levels: info, warning, error. details is a JSON blob or NULL.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time


def query(conn, after_id, limit, category, levels):
    conds, params = ["id > ?"], [after_id]
    if category:
        conds.append("category = ?")
        params.append(category)
    if levels:
        conds.append(f"level IN ({','.join('?' * len(levels))})")
        params.extend(levels)
    sql = (f"SELECT id, timestamp, level, category, message, details FROM event_log "
           f"WHERE {' AND '.join(conds)} ORDER BY id DESC LIMIT ?")
    rows = conn.execute(sql, params + [limit]).fetchall()
    return list(reversed(rows))


def show(r):
    detail = f"  {r['details']}" if r["details"] else ""
    print(f"{r['timestamp'][:19]} {r['level'].upper():<7} [{r['category']}] {r['message']}{detail}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db")
    ap.add_argument("-n", type=int, default=30, help="initial rows to show")
    ap.add_argument("--category", default=None)
    ap.add_argument("--level", default=None, help="comma-separated: info,warning,error")
    ap.add_argument("--follow", action="store_true")
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args()

    levels = [x.strip() for x in args.level.split(",")] if args.level else None
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    rows = query(conn, 0, args.n, args.category, levels)
    last_id = 0
    for r in rows:
        show(r)
        last_id = max(last_id, r["id"])

    if not args.follow:
        return 0
    try:
        while True:
            time.sleep(args.interval)
            for r in query(conn, last_id, 1000, args.category, levels):
                show(r)
                last_id = max(last_id, r["id"])
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    try:
        import signal
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # behave well under `| head`
    except (ImportError, AttributeError, ValueError):
        pass
    sys.exit(main())
