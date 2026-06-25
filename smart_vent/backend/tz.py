"""Centralized timezone handling for the backend.

The add-on's local timezone comes from the ``timezone`` add-on option, which
``run.sh`` exports as the ``TZ`` environment variable before the backend starts
(see ``config.yaml`` / ``run.sh``). Every code path that needs *local*
(wall-clock) time — schedule matching, metrics bucketing, "today" rollups —
MUST go through these helpers instead of calling ``datetime.now()`` /
``datetime.astimezone()`` directly, so there is a single, testable source of
truth for "what timezone are we in".

Storage timestamps are a separate concern: rows are written in UTC
(``datetime.now(UTC)``) and converted to local only at the display/aggregation
boundary via :func:`to_local`. Do not use these helpers to *store* time.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def get_timezone() -> tzinfo:
    """Return the add-on's configured local timezone.

    Resolved from the ``TZ`` environment variable (set by ``run.sh`` from the
    ``timezone`` add-on option). Falls back to the OS-resolved local zone, then
    UTC, when ``TZ`` is unset or names an unknown zone. ``ZoneInfo`` instances
    are cached by the standard library, so repeated calls are cheap.
    """
    name = os.environ.get("TZ")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    # No / invalid TZ: use whatever the OS resolved as local, finally UTC.
    return datetime.now().astimezone().tzinfo or UTC


def now_local() -> datetime:
    """Current moment as a timezone-aware datetime in the local zone."""
    return datetime.now(get_timezone())


def today_local() -> date:
    """Today's calendar date in the local zone."""
    return now_local().date()


def to_local(dt: datetime) -> datetime:
    """Convert ``dt`` to a timezone-aware datetime in the local zone.

    A naive ``dt`` is treated as already being local wall-clock — matching the
    semantics of ``datetime.astimezone()`` on a naive value, which this
    replaces across the backend.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=get_timezone())
    return dt.astimezone(get_timezone())


def to_local_naive(dt: datetime) -> datetime:
    """Convert ``dt`` to local wall-clock and drop the tzinfo.

    A naive ``dt`` is assumed to already be local and is returned unchanged.
    Useful for arithmetic against the naive ``datetime.combine(...)`` values
    used by the schedule helpers (subtracting a naive from an aware datetime
    raises ``TypeError``).
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(get_timezone()).replace(tzinfo=None)
