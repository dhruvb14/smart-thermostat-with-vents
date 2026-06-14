"""Temperature-unit handling for MCP write tools (Issue #284).

The MCP server runs as its own process with no live ``Scheduler``, so it cannot
call ``scheduler.get_temperature_unit()`` the way the REST handlers do. Instead
the active *display* unit is read from the persisted
``system_settings.temperature_unit`` row, which the add-on's scheduler writes on
startup and whenever HA's unit changes.

MCP temperature inputs are interpreted in that unit — mirroring the REST write
boundary and the UI — and converted to the internal °F storage representation
via the shared :mod:`backend.units` helpers, exactly as ``routes.py`` does with
``_to_f`` / ``_delta_to_f``. Storing the raw value (the previous behaviour) would
record "21" as 21 °F (≈ −6 °C) in a Celsius household — the same data-corruption
class as the #231 double-conversion bug.
"""

from __future__ import annotations

import aiosqlite

from .. import db
from ..units import from_f, from_f_delta


async def active_unit(conn: aiosqlite.Connection) -> str:
    """Return the active display unit ('F' or 'C').

    Persisted by the scheduler in ``system_settings``; defaults to 'F' (so a
    fresh DB or an imperial install converts as identity, unchanged behaviour).
    """
    return await db.get_system_setting(conn, "temperature_unit", "F")


def echo_abs(value_f: float, unit: str) -> str:
    """Echo a stored absolute °F temperature in the active unit for tool output.

    e.g. ``"21.0°C (69.8°F stored)"`` in Celsius, ``"70.0°F"`` in Fahrenheit.
    """
    if unit == "C":
        return f"{from_f(value_f, 'C')}°C ({value_f}°F stored)"
    return f"{value_f}°F"


def echo_delta(value_f: float, unit: str) -> str:
    """Echo a stored °F delta (deadband/overshoot) in the active unit.

    Deltas scale without the 32° offset, so ``echo_delta`` uses ``from_f_delta``.
    e.g. ``"1.1°C (2.0°F stored)"`` in Celsius, ``"2.0°F"`` in Fahrenheit.
    """
    if unit == "C":
        return f"{from_f_delta(value_f, 'C')}°C ({value_f}°F stored)"
    return f"{value_f}°F"
