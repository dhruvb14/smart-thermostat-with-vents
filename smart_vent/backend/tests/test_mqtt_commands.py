"""Payload decoding and REST-request building (Issue #519)."""

from __future__ import annotations

import pytest

from backend.mqtt import commands
from backend.mqtt.commands import CommandError, build_request, decode_value
from backend.mqtt.registry import (
    DEVICE_ROOM,
    DEVICE_SYSTEM,
    DEVICE_THERMOSTAT,
    SCHEDULE_CONTROL,
    control_for,
)

ROOM_ID = "room-guid"
THERMO = "climate.upstairs"


def _room(key: str):
    control = control_for(DEVICE_ROOM, key)
    assert control is not None, key
    return control


def _thermostat(key: str):
    control = control_for(DEVICE_THERMOSTAT, key)
    assert control is not None, key
    return control


def _system(key: str):
    control = control_for(DEVICE_SYSTEM, key)
    assert control is not None, key
    return control


class TestParsers:
    @pytest.mark.parametrize("payload", ["ON", "on", "true", "1", "YES"])
    def test_truthy(self, payload: str) -> None:
        assert commands.parse_bool(payload) is True

    @pytest.mark.parametrize("payload", ["OFF", "off", "false", "0", "no"])
    def test_falsy(self, payload: str) -> None:
        assert commands.parse_bool(payload) is False

    def test_bad_bool(self) -> None:
        with pytest.raises(CommandError, match="ON or OFF"):
            commands.parse_bool("maybe")

    def test_number(self) -> None:
        assert commands.parse_number(" 68.5 ") == 68.5

    def test_bad_number(self) -> None:
        with pytest.raises(CommandError, match="a number"):
            commands.parse_number("warm")

    def test_datetime_with_offset_is_normalised_to_utc(self) -> None:
        parsed = commands.parse_datetime("2026-08-01T12:00:00-04:00")
        assert parsed.isoformat() == "2026-08-01T16:00:00+00:00"

    def test_naive_datetime_uses_the_addon_timezone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A human typing a bare time into an automation means local time —
        reading it as UTC would silently shift the action by the offset."""
        monkeypatch.setenv("TZ", "America/New_York")
        parsed = commands.parse_datetime("2026-08-01T12:00:00")
        assert parsed.isoformat() == "2026-08-01T16:00:00+00:00"

    @pytest.mark.parametrize("payload", ["", "not a date", "2026-13-45"])
    def test_bad_datetime(self, payload: str) -> None:
        with pytest.raises(CommandError, match="ISO-8601"):
            commands.parse_datetime(payload)


class TestDecodeValue:
    def test_empty_payload_clears_a_nullable_field(self) -> None:
        assert decode_value(_room("deadband_override"), "") is None

    def test_empty_payload_is_an_error_on_a_required_field(self) -> None:
        with pytest.raises(CommandError):
            decode_value(_room("temp_offset"), "")

    def test_enum_rejects_an_unknown_option(self) -> None:
        with pytest.raises(CommandError, match="any_presence"):
            decode_value(_room("ambient_suppression_mode"), "sometimes")

    def test_enum_accepts_a_known_option(self) -> None:
        assert decode_value(_room("ambient_suppression_mode"), "off_schedule_only") == (
            "off_schedule_only"
        )


class TestFieldWrites:
    def test_room_field_write(self) -> None:
        call = build_request(
            _room("temp_offset"), "set", "1.5", device=DEVICE_ROOM, resolved_id=ROOM_ID
        )
        assert (call.method, call.path) == ("PUT", f"/api/rooms/{ROOM_ID}")
        assert call.body == {"temp_offset": 1.5}

    def test_temperatures_are_passed_through_unconverted(self) -> None:
        """The REST write boundary owns °C→°F. Converting here too would be the
        #231 double-conversion on a new transport."""
        call = build_request(
            _room("system_wide_temp"), "set", "16", device=DEVICE_ROOM, resolved_id=ROOM_ID
        )
        assert call.body == {"system_wide_temp": 16.0}

    def test_clear_writes_null(self) -> None:
        call = build_request(
            _room("deadband_override"), "clear", "PRESS", device=DEVICE_ROOM, resolved_id=ROOM_ID
        )
        assert call.body == {"deadband_override": None}

    def test_clear_is_refused_on_a_non_nullable_field(self) -> None:
        with pytest.raises(CommandError, match="cannot be cleared"):
            build_request(
                _room("temp_offset"), "clear", "", device=DEVICE_ROOM, resolved_id=ROOM_ID
            )

    def test_thermostat_field_write(self) -> None:
        call = build_request(
            _thermostat("deadband"), "set", "0.8", device=DEVICE_THERMOSTAT, resolved_id=THERMO
        )
        assert (call.method, call.path) == ("PUT", f"/api/thermostats/{THERMO}")
        assert call.body == {"deadband": 0.8}

    def test_thermostat_path_uses_the_real_entity_id(self) -> None:
        """Topics carry `climate_upstairs`; the REST path needs `climate.upstairs`."""
        call = build_request(
            _thermostat("min_setpoint"), "set", "62", device=DEVICE_THERMOSTAT, resolved_id=THERMO
        )
        assert call.path.endswith("climate.upstairs")


class TestSpecials:
    def test_presence_clear(self) -> None:
        call = build_request(
            _room("presence"), "clear", "PRESS", device=DEVICE_ROOM, resolved_id=ROOM_ID
        )
        assert (call.method, call.path) == (
            "DELETE",
            f"/api/rooms/{ROOM_ID}/presence/holdover",
        )

    def test_hold_set_omits_duration_to_take_the_api_default(self) -> None:
        call = build_request(_room("hold"), "set", "72", device=DEVICE_ROOM, resolved_id=ROOM_ID)
        assert (call.method, call.path) == ("POST", f"/api/rooms/{ROOM_ID}/override")
        assert call.body == {"target_temp": 72.0}
        assert "duration_hours" not in (call.body or {})

    def test_hold_clear(self) -> None:
        call = build_request(
            _room("hold"), "clear", "PRESS", device=DEVICE_ROOM, resolved_id=ROOM_ID
        )
        assert (call.method, call.path) == ("DELETE", f"/api/rooms/{ROOM_ID}/override")

    def test_schedule_toggle(self) -> None:
        call = build_request(
            SCHEDULE_CONTROL,
            "set",
            "OFF",
            device=DEVICE_ROOM,
            resolved_id=ROOM_ID,
            schedule_id="sched-1",
        )
        assert (call.method, call.path) == (
            "PUT",
            f"/api/rooms/{ROOM_ID}/schedules/sched-1",
        )
        assert call.body == {"enabled": False}

    def test_schedule_without_an_id_is_rejected(self) -> None:
        with pytest.raises(CommandError, match="schedule id"):
            build_request(SCHEDULE_CONTROL, "set", "ON", device=DEVICE_ROOM, resolved_id=ROOM_ID)

    def test_system_enabled(self) -> None:
        call = build_request(_system("enabled"), "set", "OFF", device=DEVICE_SYSTEM, resolved_id="")
        assert (call.method, call.path, call.body) == (
            "POST",
            "/api/system/enabled",
            {"enabled": False},
        )

    def test_vacation_return_at_is_the_enable_action(self) -> None:
        call = build_request(
            _system("vacation_mode/return_at"),
            "set",
            "2026-08-01T12:00:00+00:00",
            device=DEVICE_SYSTEM,
            resolved_id="",
        )
        assert (call.method, call.path) == ("POST", "/api/settings/vacation-mode")
        assert call.body == {"return_at": "2026-08-01T12:00:00+00:00"}

    def test_vacation_switch_off_disables(self) -> None:
        call = build_request(
            _system("vacation_mode"), "set", "OFF", device=DEVICE_SYSTEM, resolved_id=""
        )
        assert (call.method, call.path) == ("DELETE", "/api/settings/vacation-mode")

    def test_vacation_switch_on_is_rejected_with_guidance(self) -> None:
        """`POST /api/settings/vacation-mode` needs a future return_at, and a
        bare ON has none — so say what to set instead of failing opaquely."""
        with pytest.raises(CommandError, match="return_at"):
            build_request(
                _system("vacation_mode"), "set", "ON", device=DEVICE_SYSTEM, resolved_id=""
            )

    def test_eco_suspend_until_is_the_suspend_action(self) -> None:
        call = build_request(
            _thermostat("eco_suspend_until"),
            "set",
            "2026-08-01T12:00:00+00:00",
            device=DEVICE_THERMOSTAT,
            resolved_id=THERMO,
        )
        assert (call.method, call.path) == ("POST", f"/api/thermostats/{THERMO}/eco-suspend")

    def test_eco_suspend_switch_off_clears(self) -> None:
        call = build_request(
            _thermostat("eco_suspend"), "set", "OFF", device=DEVICE_THERMOSTAT, resolved_id=THERMO
        )
        assert (call.method, call.path) == ("DELETE", f"/api/thermostats/{THERMO}/eco-suspend")

    def test_eco_suspend_switch_on_is_rejected_with_guidance(self) -> None:
        with pytest.raises(CommandError, match="eco_suspend_until"):
            build_request(
                _thermostat("eco_suspend"),
                "set",
                "ON",
                device=DEVICE_THERMOSTAT,
                resolved_id=THERMO,
            )


class TestEncodeValue:
    def test_none_renders_as_an_empty_payload(self) -> None:
        assert commands.encode_value(_room("deadband_override"), None) == ""

    def test_bools(self) -> None:
        control = _room("include_thermostat_sensor")
        assert commands.encode_value(control, True) == "ON"
        assert commands.encode_value(control, False) == "OFF"

    def test_whole_numbers_lose_the_trailing_zero(self) -> None:
        assert commands.encode_value(_room("temp_offset"), 2.0) == "2"

    def test_fractions_are_kept(self) -> None:
        assert commands.encode_value(_room("temp_offset"), 1.5) == "1.5"

    def test_enum_passes_through(self) -> None:
        assert (
            commands.encode_value(_room("ambient_suppression_mode"), "any_presence")
            == "any_presence"
        )
