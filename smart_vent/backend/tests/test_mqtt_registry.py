"""The MQTT control registry, and its parity with the domain models (#519).

The point of this file is the two coverage tests: every writable ``Room`` and
``ThermostatConfig`` field must be either exposed on MQTT or listed in the
registry's exclusion set. Adding a field to either model therefore fails CI
until someone decides which it is, so #519's deliberate omissions — the
safety/equipment-protection cluster above all — cannot quietly erode as the
models grow.
"""

from __future__ import annotations

import dataclasses

import pytest

from backend.models import Room, ThermostatConfig
from backend.mqtt.registry import (
    CONTROLS_BY_DEVICE,
    DEVICE_ROOM,
    DEVICE_SYSTEM,
    DEVICE_THERMOSTAT,
    ROOM_CONTROLS,
    ROOM_EXCLUDED_FIELDS,
    SCHEDULE_CONTROL,
    SYSTEM_CONTROLS,
    THERMOSTAT_CONTROLS,
    THERMOSTAT_EXCLUDED_FIELDS,
    Control,
    control_for,
)

# The cluster #519 keeps off MQTT on trust-boundary grounds: broker ACLs are a
# weaker gate than the require_auth-protected UI/MCP, and these guard against
# real equipment damage. Spelled out again here so a future edit to the registry
# that "tidies up" the exclusion set has to argue with a named test.
SAFETY_FIELDS = frozenset(
    {
        "min_cycle_runtime_min",
        "min_cycle_offtime_min",
        "cycle_timeout_hours",
        "min_open_vents_fraction",
        "max_vent_closed_min",
        "unavailable_abort_after_min",
        "overflow_during_min_runtime",
        "reconciliation_interval_min",
    }
)


def _model_fields(model) -> set[str]:
    return {f.name for f in dataclasses.fields(model)}


def _exposed(controls) -> set[str]:
    return {c.field for c in controls if c.field is not None}


class TestModelParity:
    def test_every_room_field_is_exposed_or_excluded(self) -> None:
        unaccounted = _model_fields(Room) - _exposed(ROOM_CONTROLS) - ROOM_EXCLUDED_FIELDS
        assert not unaccounted, (
            f"Room fields {sorted(unaccounted)} are neither exposed on MQTT nor listed in "
            "ROOM_EXCLUDED_FIELDS. Add a Control for each, or add it to the exclusion set "
            "with a reason (see registry.py's module docstring)."
        )

    def test_every_thermostat_field_is_exposed_or_excluded(self) -> None:
        unaccounted = (
            _model_fields(ThermostatConfig)
            - _exposed(THERMOSTAT_CONTROLS)
            - THERMOSTAT_EXCLUDED_FIELDS
        )
        assert not unaccounted, (
            f"ThermostatConfig fields {sorted(unaccounted)} are neither exposed on MQTT nor "
            "listed in THERMOSTAT_EXCLUDED_FIELDS."
        )

    def test_exclusion_sets_only_name_real_fields(self) -> None:
        """A stale exclusion silently stops guarding anything."""
        assert ROOM_EXCLUDED_FIELDS.issubset(_model_fields(Room))
        assert THERMOSTAT_EXCLUDED_FIELDS.issubset(_model_fields(ThermostatConfig))

    def test_no_field_is_both_exposed_and_excluded(self) -> None:
        assert not _exposed(ROOM_CONTROLS) & ROOM_EXCLUDED_FIELDS
        assert not _exposed(THERMOSTAT_CONTROLS) & THERMOSTAT_EXCLUDED_FIELDS

    def test_safety_fields_stay_off_mqtt(self) -> None:
        """#519's central security decision, pinned as a test."""
        assert SAFETY_FIELDS <= THERMOSTAT_EXCLUDED_FIELDS
        assert not SAFETY_FIELDS & _exposed(THERMOSTAT_CONTROLS)

    def test_room_name_is_not_a_control(self) -> None:
        """Names address rooms on MQTT; making one writable there would let an
        automation move the very topic tree it is publishing to."""
        assert "name" not in _exposed(ROOM_CONTROLS)


class TestRegistryShape:
    @pytest.mark.parametrize(
        "control",
        [*ROOM_CONTROLS, *THERMOSTAT_CONTROLS, *SYSTEM_CONTROLS, SCHEDULE_CONTROL],
        ids=lambda c: f"{c.entity}:{c.key}",
    )
    def test_control_is_well_formed(self, control: Control) -> None:
        assert control.key and control.name
        assert (control.field is None) != (control.special is None)
        if control.entity == "select":
            assert len(control.options) >= 2
        if control.entity == "number":
            assert control.min is not None and control.max is not None
            assert control.min < control.max
        # A temperature is either absolute or a delta — the #231 distinction. A
        # third value would silently pick the delta branch on conversion.
        assert control.temp in (None, "absolute", "delta")

    def test_keys_are_unique_per_device(self) -> None:
        for device, controls in CONTROLS_BY_DEVICE.items():
            keys = [c.key for c in controls]
            assert len(keys) == len(set(keys)), f"duplicate control key on {device}"

    def test_actions_have_no_state_and_only_a_clear_verb(self) -> None:
        for control in [*ROOM_CONTROLS, *THERMOSTAT_CONTROLS, *SYSTEM_CONTROLS]:
            if control.kind == "action":
                assert control.has_state is False
                assert control.verbs == ("clear",)

    def test_nullable_controls_offer_both_verbs(self) -> None:
        nullable = [c for c in ROOM_CONTROLS if c.nullable]
        assert nullable, "the nullable room fields are part of #519's v1 scope"
        for control in nullable:
            assert control.verbs == ("set", "clear")

    def test_the_hold_is_clearable_without_being_nullable(self) -> None:
        hold = control_for(DEVICE_ROOM, "hold")
        assert hold is not None
        assert hold.clearable and not hold.nullable
        assert hold.verbs == ("set", "clear")

    def test_room_eco_overrides_are_nullable_and_thermostat_ones_are_not(self) -> None:
        """Rooms inherit field by field (None = inherit); the thermostat holds
        the concrete base values, so nulling one there is meaningless."""
        for key in ("eco_cooling_max_drift", "eco_hysteresis_band", "eco_mode_enabled"):
            room_control = control_for(DEVICE_ROOM, key)
            thermostat_control = control_for(DEVICE_THERMOSTAT, key)
            assert room_control is not None and room_control.nullable
            assert thermostat_control is not None and not thermostat_control.nullable

    def test_absolute_and_delta_eco_fields_keep_their_kinds_across_devices(self) -> None:
        """A field that is absolute on the thermostat and delta on the room (or
        vice versa) would convert differently on the two state topics."""
        for control in THERMOSTAT_CONTROLS:
            twin = control_for(DEVICE_ROOM, control.key)
            if twin is not None and control.field is not None:
                assert twin.temp == control.temp, control.key

    def test_lookup_misses_return_none(self) -> None:
        assert control_for(DEVICE_ROOM, "no_such_control") is None
        assert control_for("no_such_device", "hold") is None

    def test_system_controls_are_all_specials(self) -> None:
        """None of them is a plain field write — each has its own endpoint."""
        assert all(c.special is not None for c in SYSTEM_CONTROLS)
        assert control_for(DEVICE_SYSTEM, "enabled") is not None

    def test_a_control_must_declare_exactly_one_of_field_or_special(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            Control(key="x", entity="switch", name="X", kind="bool")
        with pytest.raises(ValueError, match="exactly one"):
            Control(key="x", entity="switch", name="X", kind="bool", field="a", special="b")
