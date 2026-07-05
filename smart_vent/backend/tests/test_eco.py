"""Unit tests for the pure Eco Mode module (Issue #404).

``backend/eco.py`` holds the relaxation math, the dual-unit default table, and
the field-level null-inheritance resolver. It is pure (no I/O, no model
imports), so every branch is exercised here directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend import eco


@dataclass
class _Cfg:
    """Duck-typed thermostat config with the non-null Eco defaults."""

    eco_mode_enabled: bool = True
    eco_cooling_outdoor_threshold: float = 86.0
    eco_cooling_full_drift_temp: float = 100.0
    eco_cooling_max_drift: float = 4.0
    eco_heating_outdoor_threshold: float = 40.0
    eco_heating_full_drift_temp: float = 0.0
    eco_heating_max_drift: float = 4.0
    eco_hysteresis_band: float = 2.0


@dataclass
class _RoomCfg:
    """Duck-typed room override — every field nullable (None = inherit)."""

    eco_mode_enabled: bool | None = None
    eco_cooling_outdoor_threshold: float | None = None
    eco_cooling_full_drift_temp: float | None = None
    eco_cooling_max_drift: float | None = None
    eco_heating_outdoor_threshold: float | None = None
    eco_heating_full_drift_temp: float | None = None
    eco_heating_max_drift: float | None = None
    eco_hysteresis_band: float | None = None


def _cool(requested, outside, engaged_prev=False, cfg=None, lo=60.0, hi=85.0):
    params = eco.resolve_params(cfg or _Cfg())
    return eco.relax_target(requested, "cooling", outside, params, lo, hi, engaged_prev)


def _heat(requested, outside, engaged_prev=False, cfg=None, lo=60.0, hi=85.0):
    params = eco.resolve_params(cfg or _Cfg())
    return eco.relax_target(requested, "heating", outside, params, lo, hi, engaged_prev)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_fahrenheit_defaults_are_round_f(self):
        assert eco.eco_defaults_for_unit("F") == {
            "eco_cooling_outdoor_threshold": 86.0,
            "eco_cooling_full_drift_temp": 100.0,
            "eco_cooling_max_drift": 4.0,
            "eco_heating_outdoor_threshold": 40.0,
            "eco_heating_full_drift_temp": 0.0,
            "eco_heating_max_drift": 4.0,
            "eco_hysteresis_band": 2.0,
        }

    def test_celsius_defaults_read_round_in_celsius(self):
        c = eco.eco_defaults_for_unit("C")
        # 30/38/Δ2/4/-18/Δ2/Δ1 °C stored as °F.
        assert c["eco_cooling_outdoor_threshold"] == 86.0
        assert c["eco_cooling_full_drift_temp"] == 100.4
        assert c["eco_cooling_max_drift"] == 3.6
        assert c["eco_heating_outdoor_threshold"] == 39.2
        assert c["eco_heating_full_drift_temp"] == -0.4
        assert c["eco_heating_max_drift"] == 3.6
        assert c["eco_hysteresis_band"] == 1.8

    def test_unknown_unit_falls_back_to_fahrenheit(self):
        assert eco.eco_defaults_for_unit("K") == eco.ECO_DEFAULTS_F

    def test_returned_dict_is_a_copy(self):
        d = eco.eco_defaults_for_unit("F")
        d["eco_hysteresis_band"] = 999
        assert eco.ECO_DEFAULTS_F["eco_hysteresis_band"] == 2.0

    def test_temp_fields_tuple_matches_defaults(self):
        assert set(eco.ECO_TEMP_FIELDS) == set(eco.ECO_DEFAULTS_F)


# ---------------------------------------------------------------------------
# Cooling ramp
# ---------------------------------------------------------------------------


class TestCoolingRamp:
    def test_at_threshold_no_drift(self):
        r = _cool(70.0, 86.0)
        assert r.effective_target == 70.0
        assert r.eco_active is False
        assert r.engaged is True

    def test_midpoint_half_drift(self):
        r = _cool(70.0, 93.0)  # (93-86)/(100-86)=0.5 → +2
        assert r.effective_target == 72.0
        assert r.eco_active is True

    def test_at_full_drift_max(self):
        r = _cool(70.0, 100.0)  # f=1 → +4
        assert r.effective_target == 74.0

    def test_beyond_full_drift_clamped_to_max(self):
        assert _cool(70.0, 120.0).effective_target == 74.0

    def test_below_threshold_no_op(self):
        r = _cool(70.0, 80.0)
        assert r.effective_target == 70.0
        assert r.eco_active is False
        assert r.engaged is False

    def test_clamped_to_max_setpoint(self):
        # requested near the ceiling; drift would exceed max_setpoint.
        r = _cool(84.0, 100.0, hi=85.0)
        assert r.effective_target == 85.0
        assert r.eco_active is True

    def test_no_room_to_relax_when_at_ceiling(self):
        r = _cool(85.0, 100.0, hi=85.0)
        assert r.effective_target == 85.0
        assert r.eco_active is False  # already at the ceiling → nothing moved


# ---------------------------------------------------------------------------
# Heating ramp (mirror image)
# ---------------------------------------------------------------------------


class TestHeatingRamp:
    def test_at_threshold_no_drift(self):
        r = _heat(70.0, 40.0)
        assert r.effective_target == 70.0
        assert r.eco_active is False

    def test_midpoint_half_drift(self):
        r = _heat(70.0, 20.0)  # (40-20)/(40-0)=0.5 → -2
        assert r.effective_target == 68.0
        assert r.eco_active is True

    def test_at_full_drift_max(self):
        assert _heat(70.0, 0.0).effective_target == 66.0

    def test_beyond_full_drift_clamped(self):
        assert _heat(70.0, -20.0).effective_target == 66.0

    def test_above_threshold_no_op(self):
        r = _heat(70.0, 60.0)
        assert r.effective_target == 70.0
        assert r.engaged is False

    def test_clamped_to_min_setpoint(self):
        r = _heat(62.0, 0.0, lo=60.0)  # 62-4=58 → clamp to 60
        assert r.effective_target == 60.0
        assert r.eco_active is True


# ---------------------------------------------------------------------------
# Degenerate / step config (full_drift == threshold) + hysteresis
# ---------------------------------------------------------------------------


class TestStepAndHysteresis:
    def _step_cfg(self):
        return _Cfg(eco_cooling_full_drift_temp=86.0, eco_hysteresis_band=2.0)

    def test_step_jumps_to_full_drift_on_crossing(self):
        r = _cool(70.0, 87.0, cfg=self._step_cfg())
        assert r.effective_target == 74.0  # straight to max drift
        assert r.engaged is True

    def test_hysteresis_holds_between_band_and_threshold(self):
        # Previously engaged, outside 85 (>= 86-2=84) → stay engaged, still max.
        r = _cool(70.0, 85.0, engaged_prev=True, cfg=self._step_cfg())
        assert r.effective_target == 74.0
        assert r.engaged is True

    def test_hysteresis_disengages_below_band(self):
        r = _cool(70.0, 83.0, engaged_prev=True, cfg=self._step_cfg())
        assert r.effective_target == 70.0
        assert r.eco_active is False
        assert r.engaged is False

    def test_not_engaged_below_threshold_even_with_step(self):
        r = _cool(70.0, 85.0, engaged_prev=False, cfg=self._step_cfg())
        assert r.engaged is False
        assert r.effective_target == 70.0

    def test_heating_hysteresis_holds_above_threshold(self):
        cfg = _Cfg(eco_heating_full_drift_temp=40.0, eco_hysteresis_band=2.0)
        # engaged, outside 41 (<= 40+2=42) → stay engaged, step to max drift.
        r = _heat(70.0, 41.0, engaged_prev=True, cfg=cfg)
        assert r.effective_target == 66.0
        assert r.engaged is True


# ---------------------------------------------------------------------------
# No-op paths (the eco-off byte-identical guarantee)
# ---------------------------------------------------------------------------


class TestNoOp:
    def test_disabled_is_noop(self):
        r = _cool(70.0, 100.0, cfg=_Cfg(eco_mode_enabled=False))
        assert r == eco.EcoResult(70.0, False, False)

    def test_missing_outside_temp_is_noop(self):
        r = _cool(70.0, None)
        assert r == eco.EcoResult(70.0, False, False)

    def test_unknown_mode_is_noop(self):
        params = eco.resolve_params(_Cfg())
        r = eco.relax_target(70.0, "off", 100.0, params, 60.0, 85.0, False)
        assert r == eco.EcoResult(70.0, False, False)


# ---------------------------------------------------------------------------
# resolve_params — field-level null-inheritance + enable precedence
# ---------------------------------------------------------------------------


class TestResolveParams:
    def test_thermostat_only(self):
        p = eco.resolve_params(_Cfg())
        assert p.enabled is True
        assert p.cooling_max_drift == 4.0
        assert p.hysteresis_band == 2.0

    def test_room_none_inherits_all(self):
        p = eco.resolve_params(_Cfg(), _RoomCfg())
        assert p.cooling_max_drift == 4.0
        assert p.cooling_outdoor_threshold == 86.0

    def test_room_overrides_single_field(self):
        p = eco.resolve_params(_Cfg(), _RoomCfg(eco_cooling_max_drift=6.0))
        assert p.cooling_max_drift == 6.0  # overridden
        assert p.cooling_outdoor_threshold == 86.0  # inherited

    def test_room_enables_even_when_thermostat_off(self):
        p = eco.resolve_params(_Cfg(eco_mode_enabled=False), _RoomCfg(eco_mode_enabled=True))
        assert p.enabled is True

    def test_room_disables_even_when_thermostat_on(self):
        p = eco.resolve_params(_Cfg(eco_mode_enabled=True), _RoomCfg(eco_mode_enabled=False))
        assert p.enabled is False

    def test_room_enable_none_inherits(self):
        assert eco.resolve_params(_Cfg(eco_mode_enabled=True), _RoomCfg()).enabled is True
        assert eco.resolve_params(_Cfg(eco_mode_enabled=False), _RoomCfg()).enabled is False


# ---------------------------------------------------------------------------
# Ramp fraction helper edge cases
# ---------------------------------------------------------------------------


class TestRampFraction:
    def test_negative_span_is_step(self):
        # full_drift below threshold (misconfigured) → treated as a step.
        assert eco._ramp_fraction(5.0, -3.0) == 1.0

    def test_zero_distance(self):
        assert eco._ramp_fraction(0.0, 14.0) == 0.0

    def test_partial(self):
        assert eco._ramp_fraction(7.0, 14.0) == 0.5

    def test_over_full(self):
        assert eco._ramp_fraction(20.0, 14.0) == 1.0
