# SPDX-License-Identifier: AGPL-3.0-or-later
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

import logging
import os
from datetime import UTC, date, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)


def now_utc() -> datetime:
    """Current moment as a timezone-aware UTC datetime.

    Normally just ``datetime.now(UTC)``. When ``PLENUM_CLOCK_OVERRIDE`` is set
    (an ISO-8601 instant, ideally with an explicit offset — a naive value is
    read as UTC), this returns that fixed instant instead. The override exists
    solely to make the E2E visual-regression goldens deterministic: the room
    active-status read path (schedule active/idle, "next schedule" label) is a
    pure function of "now", so pinning it stops the goldens flipping between
    ``via Schedule`` and ``Not active`` with real wall-clock time (see #456,
    the backend sequel to #182's frontend ``<Frozen>`` fix).

    Scope is deliberately narrow: **only the status read path calls this.** The
    override is never set in production (it is an E2E-stack-only env var, the
    backend twin of the ``isCI`` flag), so this compiles to plain
    ``datetime.now(UTC)`` for real installs. :func:`now_local` is intentionally
    NOT wired through here — the live engine (cycle timing, holdover expiry,
    metrics rollups, the schedule-expiry sweep) must run on the real clock.
    """
    override = os.environ.get("PLENUM_CLOCK_OVERRIDE")
    if override:
        try:
            parsed = datetime.fromisoformat(override)
        except ValueError:
            log.warning("Ignoring unparseable PLENUM_CLOCK_OVERRIDE=%r", override)
        else:
            # A naive override is read as UTC; an aware one is converted to UTC.
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return datetime.now(UTC)


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


def describe_timezone() -> tuple[str, bool]:
    """Return ``(name, is_fallback)`` for the zone :func:`get_timezone` resolves.

    ``name`` is the IANA key (``"America/New_York"``) when the zone is a
    ``ZoneInfo``; otherwise the zone's fixed UTC offset as ``"UTC±HH:MM"``
    (``"UTC"`` for a zero offset), which is what the OS-fallback path yields.
    ``is_fallback`` is True when ``TZ`` was unset or named an unknown zone, so
    the UI can warn that the add-on ``timezone`` option is almost certainly
    misconfigured (Issue #729). The frontend uses the name to show and enter
    every time in the house's zone rather than the browser's.
    """
    name = os.environ.get("TZ")
    zone = get_timezone()
    is_fallback = True
    if name:
        try:
            ZoneInfo(name)
            is_fallback = False
        except (ZoneInfoNotFoundError, ValueError):
            pass
    key = getattr(zone, "key", None)
    if isinstance(key, str) and key:
        return key, is_fallback
    offset = datetime.now(zone).utcoffset()
    total = int(offset.total_seconds()) if offset is not None else 0
    if total == 0:
        return "UTC", is_fallback
    sign = "+" if total > 0 else "-"
    hours, rem = divmod(abs(total), 3600)
    return f"UTC{sign}{hours:02d}:{rem // 60:02d}", is_fallback


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
