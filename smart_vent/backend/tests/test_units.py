"""Unit tests for the temperature converters in backend/units.py."""

from __future__ import annotations

from backend.units import delta_to_f, from_f, from_f_delta, to_f


class TestToF:
    def test_fahrenheit_input_is_noop(self):
        assert to_f(70.0, "F") == 70.0
        assert to_f(68.5, "F") == 68.5

    def test_fahrenheit_rounds_to_2dp(self):
        assert to_f(70.123456, "F") == 70.12

    def test_celsius_converts_freezing(self):
        assert to_f(0.0, "C") == 32.0

    def test_celsius_converts_room_temp(self):
        assert to_f(20.0, "C") == 68.0

    def test_celsius_precision_2dp(self):
        # 6.3°C → 43.34°F (not 43.3 — the 2dp storage fix)
        assert to_f(6.3, "C") == 43.34

    def test_celsius_21_degrees(self):
        assert to_f(21.0, "C") == 69.8


class TestDeltaToF:
    def test_fahrenheit_input_is_noop(self):
        assert delta_to_f(0.5, "F") == 0.5

    def test_fahrenheit_rounds_to_2dp(self):
        assert delta_to_f(1.234567, "F") == 1.23

    def test_celsius_delta_no_offset(self):
        # 0.3°C delta → 0.54°F (multiply only, no +32)
        assert delta_to_f(0.3, "C") == 0.54

    def test_celsius_delta_2_degrees(self):
        assert delta_to_f(2.0, "C") == 3.6

    def test_celsius_delta_does_not_add_offset(self):
        # +32 must NOT be applied to deltas — 1°C delta = 1.8°F, not 33.8°F
        assert delta_to_f(1.0, "C") == 1.8


class TestFromF:
    def test_fahrenheit_input_is_noop(self):
        assert from_f(70.0, "F") == 70.0
        assert from_f(68.5, "F") == 68.5

    def test_fahrenheit_rounds_to_1dp(self):
        assert from_f(70.0, "F") == 70.0
        assert from_f(72.0, "F") == 72.0

    def test_celsius_converts_freezing(self):
        assert from_f(32.0, "C") == 0.0

    def test_celsius_room_temp(self):
        # 69.8°F → 21.0°C
        assert from_f(69.8, "C") == 21.0

    def test_celsius_precision_1dp(self):
        # 72°F → 22.2°C (rounded to 1dp)
        assert from_f(72.0, "C") == 22.2

    def test_celsius_boiling(self):
        assert from_f(212.0, "C") == 100.0

    def test_none_returns_empty_string_fahrenheit(self):
        assert from_f(None, "F") == ""

    def test_none_returns_empty_string_celsius(self):
        assert from_f(None, "C") == ""


class TestFromFDelta:
    def test_fahrenheit_input_is_noop(self):
        assert from_f_delta(2.0, "F") == 2.0

    def test_fahrenheit_rounds_to_1dp(self):
        assert from_f_delta(2.34, "F") == 2.3

    def test_celsius_delta_no_offset(self):
        # 1.8°F delta → 1.0°C delta (multiply only, no -32 offset)
        assert from_f_delta(1.8, "C") == 1.0

    def test_celsius_delta_2_degrees(self):
        # 3.6°F delta → 2.0°C delta
        assert from_f_delta(3.6, "C") == 2.0

    def test_celsius_delta_does_not_subtract_offset(self):
        # A 2°F deadband is ~1.1°C, never a negative number.
        assert from_f_delta(2.0, "C") == 1.1
