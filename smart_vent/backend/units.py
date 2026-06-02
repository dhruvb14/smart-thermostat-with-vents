"""Temperature unit conversion — the single source of truth.

All temperatures are stored and reasoned about internally in °F (see Issue
#123). Exactly four conversions exist, in two axes:

* direction — display→storage (``to_f`` / ``delta_to_f``) vs storage→display
  (``from_f`` / ``from_f_delta``);
* kind — **absolute** temperatures (apply the 32° offset) vs **deltas** such as
  a deadband or offset (scale only, no offset).

Getting the absolute-vs-delta axis wrong silently corrupts data — a 2 °F
deadband converted as an absolute would render as a negative °C number. Keeping
all four here, side by side, makes the distinction obvious and gives the API
write/read boundary and the HA ingest path one implementation to share.

``unit`` is the active display unit, ``"F"`` or ``"C"`` (anything other than
``"C"`` is treated as °F).
"""

from __future__ import annotations


def to_f(value: float, unit: str) -> float:
    """Convert an absolute temperature from the active *unit* to °F (2dp)."""
    if unit == "C":
        return round(value * 9 / 5 + 32, 2)
    return round(float(value), 2)


def delta_to_f(value: float, unit: str) -> float:
    """Convert a temperature delta (offset/deadband) from the active *unit* to °F (2dp)."""
    if unit == "C":
        return round(value * 9 / 5, 2)
    return round(float(value), 2)


def from_f(value: float | None, unit: str) -> float | str:
    """Convert a stored °F value to the active display unit (1dp). Returns '' for None."""
    if value is None:
        return ""
    if unit == "C":
        return round((value - 32) * 5 / 9, 1)
    return round(float(value), 1)


def from_f_delta(value: float, unit: str) -> float:
    """Convert a stored °F *delta* (deadband/offset) to the active display unit (1dp).

    Unlike :func:`from_f`, this applies no -32 offset — a 2 °F deadband is a
    1.1 °C deadband, not a negative number.
    """
    if unit == "C":
        return round(value * 5 / 9, 1)
    return round(float(value), 1)
