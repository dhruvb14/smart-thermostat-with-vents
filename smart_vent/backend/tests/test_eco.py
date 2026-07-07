"""Unit tests for the pure Eco Mode module (Issue #404).

``backend/eco.py`` holds the relaxation math, the dual-unit default table, and
the field-level null-inheritance resolver. It is pure (no I/O, no model
imports), so every branch is exercised here directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

from backend import eco
from backend.engine.cycle_engine import CycleEngine
from backend.engine.room_manager import ActiveRoom
from backend.models import Room, ThermostatConfig


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
# Whole-degree rounding (thermostats reject partial-degree setpoints)
# ---------------------------------------------------------------------------


class TestWholeDegreeRounding:
    """The relaxed target rounds to the closest whole degree, halves up.

    Most thermostats floor or reject partial-degree setpoints; commanding
    70.28°F parks the device at 70°F and the reconciler then re-asserts the
    unreachable fraction forever (the production drift-churn this fixes).
    """

    def test_round_whole_f_halves_up_not_bankers(self):
        # round() would give 70 for 70.5 (half-to-even) — the contract is .5 UP.
        assert eco.round_whole_f(70.5) == 71.0
        assert eco.round_whole_f(71.5) == 72.0
        assert eco.round_whole_f(70.49) == 70.0
        assert eco.round_whole_f(70.0) == 70.0

    def test_cooling_mid_ramp_rounds_down(self):
        # f = (91-86)/14 → +1.43 → raw 71.43 → 71.
        r = _cool(70.0, 91.0)
        assert r.effective_target == 71.0
        assert r.eco_active is True

    def test_cooling_mid_ramp_rounds_up(self):
        # f = (95-86)/14 → +2.57 → raw 72.57 → 73.
        r = _cool(70.0, 95.0)
        assert r.effective_target == 73.0
        assert r.eco_active is True

    def test_cooling_exact_half_rounds_up(self):
        # f = (94.75-86)/14 = 0.625 → +2.5 → raw 72.5 → 73.
        assert _cool(70.0, 94.75).effective_target == 73.0

    def test_heating_mid_ramp_rounds_half_up_toward_requested(self):
        # f = (40-25)/40 = 0.375 → −1.5 → raw 68.5 → 69 (plain half-UP, even
        # though that is back toward the requested target).
        r = _heat(70.0, 25.0)
        assert r.effective_target == 69.0
        assert r.eco_active is True

    def test_heating_mid_ramp_rounds_down(self):
        # f = (40-24)/40 = 0.4 → −1.6 → raw 68.4 → 68.
        assert _heat(70.0, 24.0).effective_target == 68.0

    def test_tiny_relaxation_collapses_to_requested_but_stays_eco_active(self):
        # f = (87-86)/14 → +0.29 → raw 70.29 → rounds back to the requested 70.
        # eco_active must survive the collapse: the UI keeps its 🌿 badge so
        # the user knows Eco is engaged and the number was rounded.
        r = _cool(70.0, 87.0)
        assert r.effective_target == 70.0
        assert r.eco_active is True
        assert r.engaged is True

    def test_rounding_never_escapes_the_envelope(self):
        # Fractional ceiling 71.5: raw clamps to 71.5, rounding up would give
        # 72 — the envelope clamp is re-applied and wins.
        r = _cool(70.0, 100.0, hi=71.5)
        assert r.effective_target == 71.5
        assert r.eco_active is True

    def test_no_op_paths_do_not_round(self):
        # A disengaged/disabled evaluation returns the requested value
        # untouched — fractional requests pass through (the eco-off
        # byte-identical guarantee).
        assert _cool(70.4, 80.0).effective_target == 70.4
        assert _cool(70.4, 100.0, cfg=_Cfg(eco_mode_enabled=False)).effective_target == 70.4


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


class TestEngineApplyEco:
    """Regression tests for the engine's ``_apply_eco`` (hysteresis state)."""

    def _engine(self) -> CycleEngine:
        # _apply_eco is pure computation over self._eco_engaged; ha/vent are
        # never touched, so mocks suffice and no I/O happens.
        return CycleEngine("climate.x", ha=MagicMock(), vent_ctrl=MagicMock())

    def _tc(self) -> ThermostatConfig:
        # Hard-step config (full-drift == threshold) for both modes, where a
        # stale engaged flag would be maximally visible.
        return ThermostatConfig(
            "climate.x",
            eco_mode_enabled=True,
            eco_cooling_outdoor_threshold=86.0,
            eco_cooling_full_drift_temp=86.0,
            eco_cooling_max_drift=4.0,
            eco_heating_outdoor_threshold=40.0,
            eco_heating_full_drift_temp=40.0,
            eco_heating_max_drift=4.0,
            eco_hysteresis_band=2.0,
            min_setpoint=60.0,
            max_setpoint=85.0,
        )

    def test_engaged_state_does_not_leak_across_modes(self):
        """A cooling engagement must not seed a later heating evaluation. With
        the hard-step config, a stale flag would relax a heating target while
        outside is still above the heating threshold (no relaxation is due)."""
        eng = self._engine()
        tc = self._tc()
        room = Room.create("Bed", "climate.x")

        # Hot afternoon → cooling engages and relaxes to the full drift.
        cool = ActiveRoom(room=room, target_temp=70.0, source="schedule")
        eng._apply_eco({room.id: cool}, "cooling", 95.0, tc)
        assert cool.eco_active is True
        assert cool.target_temp == 74.0
        assert eng._eco_engaged[(room.id, "cooling")] is True

        # Evening heating cycle at 41 °F — ABOVE the 40 °F heating threshold, so
        # heating must NOT relax. The stale cooling engagement must not leak.
        heat = ActiveRoom(room=room, target_temp=70.0, source="schedule")
        eng._apply_eco({room.id: heat}, "heating", 41.0, tc)
        assert heat.eco_active is False
        assert heat.target_temp == 70.0
        assert eng._eco_engaged[(room.id, "heating")] is False

    def test_heating_hysteresis_persists_within_mode(self):
        """Within one mode, engagement is held across boundaries (hysteresis)."""
        eng = self._engine()
        tc = self._tc()
        room = Room.create("Bed", "climate.x")

        cold = ActiveRoom(room=room, target_temp=70.0, source="schedule")
        eng._apply_eco({room.id: cold}, "heating", 38.0, tc)  # below threshold
        assert cold.target_temp == 66.0  # relaxed to full step
        # A later boundary at 41 (within threshold+band=42) stays engaged.
        held = ActiveRoom(room=room, target_temp=70.0, source="schedule")
        eng._apply_eco({room.id: held}, "heating", 41.0, tc)
        assert held.target_temp == 66.0  # held by hysteresis


class TestRampWithBand:
    """#420: for RAMP configs the hysteresis band keeps ``engaged`` latched but
    yields zero drift inside the band (fraction 0 below the threshold) — the
    band only changes the *target* for hard-step configs. Pin it so a change
    to _ramp_fraction's boundary handling cannot slip through unnoticed."""

    def test_engaged_in_band_yields_zero_drift_but_stays_engaged(self):
        params = eco.EcoParams(
            enabled=True,
            cooling_outdoor_threshold=86.0,
            cooling_full_drift_temp=100.0,
            cooling_max_drift=4.0,
            heating_outdoor_threshold=40.0,
            heating_full_drift_temp=0.0,
            heating_max_drift=4.0,
            hysteresis_band=2.0,
        )
        # Engaged previously; outside now 85 — inside [84, 86).
        result = eco.relax_target(70.0, "cooling", 85.0, params, 62.0, 78.0, engaged_prev=True)
        assert result.engaged is True, "inside the band the engagement must latch"
        assert result.effective_target == 70.0, "ramp fraction is 0 below threshold"
        assert result.eco_active is False

    def test_not_engaged_in_band_without_prior_engagement(self):
        params = eco.EcoParams(
            enabled=True,
            cooling_outdoor_threshold=86.0,
            cooling_full_drift_temp=100.0,
            cooling_max_drift=4.0,
            heating_outdoor_threshold=40.0,
            heating_full_drift_temp=0.0,
            heating_max_drift=4.0,
            hysteresis_band=2.0,
        )
        result = eco.relax_target(70.0, "cooling", 85.0, params, 62.0, 78.0, engaged_prev=False)
        assert result.engaged is False, "the band only holds an EXISTING engagement"
