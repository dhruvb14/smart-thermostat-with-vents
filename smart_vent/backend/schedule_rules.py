"""Schedule rules shared by every write boundary.

The REST handlers and the MCP tools both create and edit schedule blocks, and
they must agree on what a valid block is. When they disagreed before, the MCP
side silently persisted values the UI would have rejected — #284, where MCP
write tools skipped the temperature conversion contract and corrupted data in
Celsius homes. Anything a boundary must enforce about a schedule belongs here,
imported by both, rather than reimplemented on each side.

Deliberately free of any transport concern: no aiohttp, no MCP types, no
response shaping. Callers turn these results into whatever their layer needs.
"""

from __future__ import annotations

import re
from datetime import datetime

from . import tz
from .engine import room_manager
from .models import Schedule

# Longest accepted schedule display name (Issue #520). Generous for "Weekday
# night setback" and short of anything that would blow out the Schedules table
# or an HA entity's friendly name, which is the value's other consumer (#519).
MAX_NAME_LENGTH = 64

_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_name(raw: object) -> str | None:
    """Normalize a request ``name`` into a stored display name (Issue #520).

    Accepts ``None``/``""``/whitespace-only (all meaning "unnamed" → ``None``)
    or a string. Surrounding whitespace is stripped and internal runs — spaces,
    tabs, an accidentally pasted newline — collapse to single spaces, so the
    stored value is always a single-line label that renders identically in a
    table cell and in an HA friendly name. Raises TypeError for a non-string and
    ValueError past ``MAX_NAME_LENGTH``.

    Length is measured AFTER normalization: what is stored is what is bounded,
    so trailing whitespace can never push an otherwise-fine name over the limit.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise TypeError("name must be a string or null")
    cleaned = _WHITESPACE_RUN.sub(" ", raw).strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_NAME_LENGTH:
        raise ValueError(f"name must be at most {MAX_NAME_LENGTH} characters")
    return cleaned


def parse_expires_at(raw: object) -> datetime | None:
    """Parse a request ``expires_at`` into a naive LOCAL datetime.

    Accepts ``None``/``""`` (never expire), a naive local ISO string (what
    ``<input type="datetime-local">`` sends), or an aware ISO string, converted
    to local-naive. Raises ValueError/TypeError on anything else.

    Naive LOCAL is deliberate and load-bearing: it matches ``start_time`` /
    ``end_time`` so a block's expiry is in the same frame as its window.
    Treating it as UTC shifts every expiry by the timezone offset.
    """
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise TypeError("expires_at must be a string or null")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is not None:
        dt = tz.to_local_naive(dt)
    return dt


def find_conflict(
    candidate: Schedule,
    existing: list[Schedule],
    *,
    exclude_id: str | None = None,
) -> Schedule | None:
    """The first ENABLED block ``candidate`` overlaps, or None.

    Only enabled blocks reserve their slot (#359) — a parked block is inert, and
    that is what lets a room keep, say, a wide-drift night block and a tight
    guest block for the same window and flip between them. A disabled candidate
    conflicts with nothing, so callers should skip the check entirely for one;
    this returns None for it regardless, so calling anyway is safe.

    ``exclude_id`` skips the candidate's own row when re-checking an edit.
    """
    if not candidate.enabled:
        return None
    for other in existing:
        if other.id == exclude_id or other.id == candidate.id:
            continue
        if not other.enabled:
            continue
        if room_manager.schedules_overlap(candidate, other):
            return other
    return None


def describe_block(s: Schedule) -> str:
    """Human-readable "Mon, Tue 22:00–07:00" for conflict messages."""
    days = ", ".join(room_manager.DAYS_SHORT[d] for d in sorted(s.days_of_week))
    return f"{days} {s.start_time.strftime('%H:%M')}–{s.end_time.strftime('%H:%M')}"


def expiry_in_past(s: Schedule) -> bool:
    """Whether an enabled block carries an expiry that has already passed.

    A block in this state would be switched off by the very next sweep, so
    accepting one just to disable it moments later is a confusing no-op. Both
    boundaries reject it at write time instead.
    """
    return (
        s.enabled
        and s.expires_at is not None
        and s.expires_at <= tz.now_local().replace(tzinfo=None)
    )
