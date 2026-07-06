"""Eco Mode — outdoor-temperature-compensated setpoint drift (Issue #404).

Eco Mode relaxes a room's requested temperature target based on how extreme it
is outside, so the HVAC works less when it is fighting the biggest outdoor load.
It is configured globally per thermostat and overridable per room (field-level
null-inherit), and is symmetric for cooling and heating.

This module is intentionally **pure**: it operates entirely in °F (the #123
storage contract), performs no I/O, and imports no domain models. The
relaxation math and the dual-unit default table therefore have one home that is
trivially unit-testable and shared by both the engine (``cycle_engine.py``) and
the API/DB seeding path (``db.py``). See ``docs/eco-mode.md``.

KEEP IN SYNC: ``frontend/src/eco.ts`` re-implements the ramp for the UI's
worked-example preview. There is no parity test tying the two — if you change
the relaxation math here, change it there in the same PR (#420).

Drift model — a proportional ramp. Relaxation scales with how far past the
threshold it is outside, reaching the configured ``max_drift`` at a
configurable "full-drift" outdoor temperature. Cooling relaxes the target
*upward* (warmer), heating relaxes it *downward* (cooler), so the two targets
always move apart — Eco Mode can never create an opposite-cycle heat↔cool
conflict. When ``full_drift_temp`` equals the threshold the ramp degenerates to
a hard step (jump straight to the full drift once the threshold is crossed).
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Defaults (stored in °F per the #123 contract)
# ---------------------------------------------------------------------------
#
# A single stored °F value cannot read round in both units, so default seeding
# is unit-aware: on first init we store the °F value whose *display* reads round
# in whichever unit is active (see ``eco_defaults_for_unit``). A later unit
# switch never rewrites already-stored values.

#: Round-in-Fahrenheit defaults — the canonical dataclass defaults.
ECO_DEFAULTS_F: dict[str, float] = {
    "eco_cooling_outdoor_threshold": 86.0,
    "eco_cooling_full_drift_temp": 100.0,
    "eco_cooling_max_drift": 4.0,
    "eco_heating_outdoor_threshold": 40.0,
    "eco_heating_full_drift_temp": 0.0,
    "eco_heating_max_drift": 4.0,
    "eco_hysteresis_band": 2.0,
}

#: Round-in-Celsius defaults, expressed as the stored °F value. These are the
#: round °C numbers (30 / 38 / Δ2 / 4 / −18 / Δ2 / Δ1) put through the same
#: ``to_f`` / ``delta_to_f`` conversion the write boundary uses, so a °C-mode
#: user sees clean round numbers while storage stays °F.
ECO_DEFAULTS_C: dict[str, float] = {
    "eco_cooling_outdoor_threshold": 86.0,  # 30 °C
    "eco_cooling_full_drift_temp": 100.4,  # 38 °C
    "eco_cooling_max_drift": 3.6,  # Δ2 °C
    "eco_heating_outdoor_threshold": 39.2,  # 4 °C
    "eco_heating_full_drift_temp": -0.4,  # −18 °C
    "eco_heating_max_drift": 3.6,  # Δ2 °C
    "eco_hysteresis_band": 1.8,  # Δ1 °C
}

#: The seven numeric Eco config fields, in canonical order. ``eco_mode_enabled``
#: is a boolean and is handled separately (it is not a temperature field).
ECO_TEMP_FIELDS: tuple[str, ...] = tuple(ECO_DEFAULTS_F.keys())


def eco_defaults_for_unit(unit: str) -> dict[str, float]:
    """Return the °F values to seed for a config created while *unit* is active.

    ``"C"`` → the round-in-Celsius set; anything else → the round-in-Fahrenheit
    set. A fresh copy is returned so callers can mutate it freely.
    """
    return dict(ECO_DEFAULTS_C if unit == "C" else ECO_DEFAULTS_F)


# ---------------------------------------------------------------------------
# Effective (resolved) parameters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EcoParams:
    """The effective Eco configuration for one room after null-inheritance."""

    enabled: bool
    cooling_outdoor_threshold: float
    cooling_full_drift_temp: float
    cooling_max_drift: float
    heating_outdoor_threshold: float
    heating_full_drift_temp: float
    heating_max_drift: float
    hysteresis_band: float


# Maps an ``EcoParams`` attribute to the config field name it reads.
_PARAM_TO_FIELD: dict[str, str] = {
    "cooling_outdoor_threshold": "eco_cooling_outdoor_threshold",
    "cooling_full_drift_temp": "eco_cooling_full_drift_temp",
    "cooling_max_drift": "eco_cooling_max_drift",
    "heating_outdoor_threshold": "eco_heating_outdoor_threshold",
    "heating_full_drift_temp": "eco_heating_full_drift_temp",
    "heating_max_drift": "eco_heating_max_drift",
    "hysteresis_band": "eco_hysteresis_band",
}


def resolve_params(tc: object, room: object | None = None) -> EcoParams:
    """Resolve the effective Eco config with **field-level null-inheritance**.

    Each room field that is ``None`` inherits the thermostat value for that
    field; a non-``None`` room field overrides just that field. ``room=None``
    yields the pure thermostat configuration. The enable flag follows the same
    rule — a room may enable Eco even if its thermostat has it off (and vice
    versa); ``None`` inherits.

    Duck-typed on attribute names so it never imports the domain models.
    """

    def pick(field: str, default: float | bool) -> float | bool:
        tc_val = getattr(tc, field, default)
        if room is None:
            return tc_val
        room_val = getattr(room, field, None)
        return tc_val if room_val is None else room_val

    return EcoParams(
        enabled=bool(pick("eco_mode_enabled", False)),
        **{
            attr: float(pick(field, ECO_DEFAULTS_F[field]))
            for attr, field in _PARAM_TO_FIELD.items()
        },
    )


# ---------------------------------------------------------------------------
# The relaxation math
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EcoResult:
    """Outcome of a single relaxation evaluation (all temperatures in °F)."""

    effective_target: float
    eco_active: bool
    engaged: bool


def _ramp_fraction(distance_past: float, span: float) -> float:
    """Proportional ramp fraction in ``[0, 1]``.

    ``distance_past`` is how far past the threshold it is outside (°F, positive
    when past). ``span`` is the distance from threshold to the full-drift
    temperature. A ``span <= 0`` is the degenerate/step case (full-drift at or
    on the wrong side of the threshold): an engaged crossing jumps straight to
    the full drift.
    """
    if span <= 0:
        return 1.0
    if distance_past <= 0:
        return 0.0
    if distance_past >= span:
        return 1.0
    return distance_past / span


def relax_target(
    requested_target: float,
    mode: str,
    outside_f: float | None,
    params: EcoParams,
    min_setpoint: float,
    max_setpoint: float,
    engaged_prev: bool,
) -> EcoResult:
    """Compute the eco-relaxed effective target (°F). Pure — no I/O.

    When Eco is disabled, the outside temperature is missing, or the mode is not
    ``"cooling"``/``"heating"``, this is a strict **no-op**: ``effective_target
    == requested_target``, ``eco_active`` is ``False``, ``engaged`` is ``False``
    — the caller then follows the exact pre-Eco code path.

    ``engaged_prev`` carries the previous hysteresis state for this room so the
    relaxation begins at the threshold but only stops once outside falls to
    ``threshold − band`` (cooling) / rises to ``threshold + band`` (heating).
    The effective target is clamped into ``[min_setpoint, max_setpoint]``.
    """
    if not params.enabled or outside_f is None or mode not in ("cooling", "heating"):
        return EcoResult(requested_target, False, False)

    if mode == "cooling":
        threshold = params.cooling_outdoor_threshold
        full_drift_temp = params.cooling_full_drift_temp
        max_drift = params.cooling_max_drift
        band = params.hysteresis_band
        # Hysteresis: engage at/above the threshold; once engaged, stay engaged
        # until outside falls below (threshold − band).
        engaged = outside_f >= (threshold - band) if engaged_prev else outside_f >= threshold
        if not engaged:
            return EcoResult(requested_target, False, False)
        fraction = _ramp_fraction(outside_f - threshold, full_drift_temp - threshold)
        effective = requested_target + fraction * max_drift
        effective = round(min(max(effective, min_setpoint), max_setpoint), 2)
        # Cooling relaxes the target warmer; a clamp at max_setpoint (or a
        # requested target already at the ceiling) can leave it unchanged.
        return EcoResult(effective, effective > requested_target, engaged)

    # heating — mirror image, relaxing the target cooler.
    threshold = params.heating_outdoor_threshold
    full_drift_temp = params.heating_full_drift_temp
    max_drift = params.heating_max_drift
    band = params.hysteresis_band
    engaged = outside_f <= (threshold + band) if engaged_prev else outside_f <= threshold
    if not engaged:
        return EcoResult(requested_target, False, False)
    fraction = _ramp_fraction(threshold - outside_f, threshold - full_drift_temp)
    effective = requested_target - fraction * max_drift
    effective = round(min(max(effective, min_setpoint), max_setpoint), 2)
    return EcoResult(effective, effective < requested_target, engaged)
