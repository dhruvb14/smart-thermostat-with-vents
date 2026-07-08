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

import math
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


def round_whole_f(value: float) -> float:
    """Round to the closest whole degree, halves up (70.5 → 71, 70.49 → 70).

    Most thermostats reject or silently floor partial-degree setpoints, so any
    temperature the system commands to the device must be a whole number —
    otherwise the reconciler sees permanent "drift" between what we asked for
    (70.28°F) and what the device stored (70°F) and re-asserts forever.
    ``round()`` is deliberately avoided: it rounds halves to even (banker's
    rounding), and the user-facing contract is plain half-up.

    Used ONLY at the device-command boundary, and only where the command has no
    run direction (``_parked_setpoint``'s idle parking). Directional cycle
    setpoints use ``floor_whole_f`` / ``ceil_whole_f`` instead, and none of the
    three may be applied to a room's eco-relaxed effective target: that value
    is the cycle's stop condition, and rounding it makes the zone run past (or
    stop short of) the true relaxed ask.
    """
    return float(math.floor(value + 0.5))


# Tolerance for float noise at whole-degree boundaries: a computed 71.9999999
# (from e.g. repeated °C↔°F round-trips) must floor to 72, not 71.
_WHOLE_EPS = 1e-9


def floor_whole_f(value: float) -> float:
    """Round DOWN to a whole degree (float-noise tolerant).

    Cooling cycle setpoints round down: the commanded setpoint must never sit
    ABOVE the coldest room's (possibly fractional) target, or the HVAC stops
    early and the room can never reach it — visible at overshoot_delta=0,
    where closest-whole rounding of a 72.57 °F target would command 73 °F.
    """
    return float(math.floor(value + _WHOLE_EPS))


def ceil_whole_f(value: float) -> float:
    """Round UP to a whole degree (float-noise tolerant).

    Heating cycle setpoints round up — the mirror image of ``floor_whole_f``:
    the commanded setpoint must never sit BELOW the warmest room's target.
    """
    return float(math.ceil(value - _WHOLE_EPS))


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
    The effective target is clamped into ``[min_setpoint, max_setpoint]`` and
    kept at 2dp precision — it is deliberately NOT rounded to a whole degree.
    The effective target is the room's stop condition (the cycle runs until the
    room reaches it); whole-degree rounding belongs only to the device-command
    boundary, where ``_set_thermostat_setpoint`` / ``_parked_setpoint`` apply
    ``round_whole_f`` to whatever they send to HA.
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
        # The fractional relaxed target IS the room's effective ask — the cycle
        # runs until the room reaches it. It stays fractional on purpose:
        # whole-degree rounding happens only at the device-command boundary
        # (``_set_thermostat_setpoint`` / ``_parked_setpoint`` round what they
        # send to HA), so keeping the fraction here cannot re-create the
        # reconcile drift that rounding was introduced for.
        effective = round(
            min(max(requested_target + fraction * max_drift, min_setpoint), max_setpoint), 2
        )
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
    # Fractional-effective-target semantics: see the cooling branch.
    effective = round(
        min(max(requested_target - fraction * max_drift, min_setpoint), max_setpoint), 2
    )
    return EcoResult(effective, effective < requested_target, engaged)
