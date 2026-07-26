"""The one sanitisation rule shared by every MQTT identifier (Issue #519).

Topic segments must be predictable enough to hand-type into a Home Assistant
automation, so the same transformation is applied to all three places a
free-form string becomes part of a topic or an HA ``unique_id``:

* the instance topic prefix (from the add-on slug, an override, or the
  standalone-Docker default),
* room names (which also makes sanitised-name uniqueness a Room invariant),
* thermostat HA ``entity_id``\\ s — ``climate.upstairs`` → ``climate_upstairs``.

The rule: lower-case, keep ``[a-z0-9_-]``, collapse every run of anything else
into a single ``_``, and trim separators from the ends. It is deliberately
lossy and therefore *stricter* than raw-string uniqueness — ``"Office"`` and
``"office"`` both sanitise to ``office`` and so collide.
"""

from __future__ import annotations

import re

# Any run of characters outside the allowed set collapses to a single "_".
_DISALLOWED_RUN = re.compile(r"[^a-z0-9_-]+")
# Separators are meaningless at the ends of a segment.
_EDGE_SEPARATORS = re.compile(r"^[_-]+|[_-]+$")


def sanitize(raw: str) -> str:
    """Return the canonical topic-segment form of *raw*.

    Returns ``""`` when *raw* holds nothing usable (empty, or punctuation only).
    Callers decide what an empty result means: :func:`resolve_prefix` falls back
    to a default, while the room-name write boundary rejects it outright.
    """
    if not raw:
        return ""
    collapsed = _DISALLOWED_RUN.sub("_", raw.strip().lower())
    return _EDGE_SEPARATORS.sub("", collapsed)


def sanitize_entity_id(entity_id: str) -> str:
    """Sanitise an HA ``entity_id`` for use as a topic segment.

    Just :func:`sanitize` under a clearer name — ``climate.upstairs`` becomes
    ``climate_upstairs`` — kept separate so call sites read as intent, not as a
    coincidence that the two rules happen to match today.
    """
    return sanitize(entity_id)


def dedupe_name(desired: str, taken: set[str]) -> str:
    """Return a display name whose sanitised form is not already in *taken*.

    Appends ``" (2)"``, ``" (3)"``… to *desired* until its sanitised form is
    free, which is what the startup migration uses to break legacy collisions
    (``"Office"`` → ``"Office (2)"``). Deterministic: the same input set always
    produces the same output, so a migration re-run is a no-op rather than a
    fresh round of renames.

    *taken* holds already-claimed **sanitised** keys and is not modified.
    """
    if sanitize(desired) not in taken:
        return desired
    suffix = 2
    while sanitize(f"{desired} ({suffix})") in taken:
        suffix += 1
    return f"{desired} ({suffix})"
