"""Unit tests for pure helper functions in backend/api/routes.py."""

from __future__ import annotations

from backend.api.routes import _delta_to_f, _from_f, _to_f


class TestToF:
    def test_fahrenheit_input_is_noop(self):
        assert _to_f(70.0, "F") == 70.0
        assert _to_f(68.5, "F") == 68.5

    def test_fahrenheit_rounds_to_2dp(self):
        assert _to_f(70.123456, "F") == 70.12

    def test_celsius_converts_freezing(self):
        assert _to_f(0.0, "C") == 32.0

    def test_celsius_converts_room_temp(self):
        assert _to_f(20.0, "C") == 68.0

    def test_celsius_precision_2dp(self):
        # 6.3°C → 43.34°F (not 43.3 — the 2dp storage fix)
        assert _to_f(6.3, "C") == 43.34

    def test_celsius_21_degrees(self):
        assert _to_f(21.0, "C") == 69.8


class TestDeltaToF:
    def test_fahrenheit_input_is_noop(self):
        assert _delta_to_f(0.5, "F") == 0.5

    def test_fahrenheit_rounds_to_2dp(self):
        assert _delta_to_f(1.234567, "F") == 1.23

    def test_celsius_delta_no_offset(self):
        # 0.3°C delta → 0.54°F (multiply only, no +32)
        assert _delta_to_f(0.3, "C") == 0.54

    def test_celsius_delta_2_degrees(self):
        assert _delta_to_f(2.0, "C") == 3.6

    def test_celsius_delta_does_not_add_offset(self):
        # +32 must NOT be applied to deltas — 1°C delta = 1.8°F, not 33.8°F
        assert _delta_to_f(1.0, "C") == 1.8


class TestFromF:
    def test_fahrenheit_input_is_noop(self):
        assert _from_f(70.0, "F") == 70.0
        assert _from_f(68.5, "F") == 68.5

    def test_fahrenheit_rounds_to_1dp(self):
        assert _from_f(70.0, "F") == 70.0
        assert _from_f(72.0, "F") == 72.0

    def test_celsius_converts_freezing(self):
        assert _from_f(32.0, "C") == 0.0

    def test_celsius_room_temp(self):
        # 69.8°F → 21.0°C
        assert _from_f(69.8, "C") == 21.0

    def test_celsius_precision_1dp(self):
        # 72°F → 22.2°C (rounded to 1dp)
        assert _from_f(72.0, "C") == 22.2

    def test_celsius_boiling(self):
        assert _from_f(212.0, "C") == 100.0

    def test_none_returns_empty_string_fahrenheit(self):
        assert _from_f(None, "F") == ""

    def test_none_returns_empty_string_celsius(self):
        assert _from_f(None, "C") == ""
