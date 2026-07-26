"""Unit tests for the shared schedule write rules (``backend/schedule_rules``).

The module exists so the REST and MCP boundaries enforce ONE set of rules
(#284, #522). These test the pure helpers directly; the per-boundary tests then
only have to prove each boundary calls them.
"""

from __future__ import annotations

import pytest

from backend import schedule_rules


class TestNormalizeName:
    """``normalize_name`` — the optional schedule display name (Issue #520)."""

    def test_none_is_unnamed(self) -> None:
        assert schedule_rules.normalize_name(None) is None

    @pytest.mark.parametrize("raw", ["", "   ", "\t", "\n", " \t\n "])
    def test_blank_and_whitespace_only_are_unnamed(self, raw: str) -> None:
        """Blank must collapse to None, not to "". A stored empty string would
        be a third state that every caller then has to special-case, and the
        `name or id` fallback would silently paper over it."""
        assert schedule_rules.normalize_name(raw) is None

    def test_plain_name_passes_through(self) -> None:
        assert schedule_rules.normalize_name("Night setback") == "Night setback"

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert schedule_rules.normalize_name("  Night setback\n") == "Night setback"

    @pytest.mark.parametrize(
        "raw",
        ["Night  setback", "Night\tsetback", "Night\nsetback", "Night \n setback"],
    )
    def test_internal_whitespace_runs_collapse_to_one_space(self, raw: str) -> None:
        """A pasted multi-line value must not become a multi-line label — it
        renders in a table cell and (per #519) in an HA friendly name."""
        assert schedule_rules.normalize_name(raw) == "Night setback"

    def test_unicode_is_preserved(self) -> None:
        assert schedule_rules.normalize_name(" Chambre — nuit ") == "Chambre — nuit"

    def test_name_at_the_length_limit_is_accepted(self) -> None:
        at_limit = "x" * schedule_rules.MAX_NAME_LENGTH
        assert schedule_rules.normalize_name(at_limit) == at_limit

    def test_name_over_the_length_limit_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            schedule_rules.normalize_name("x" * (schedule_rules.MAX_NAME_LENGTH + 1))

    def test_length_is_measured_after_normalization(self) -> None:
        """Padding is not content: a name that only exceeds the bound because
        of whitespace it will never store must be accepted, since what gets
        stored is inside the bound."""
        padded = "  " + "x" * schedule_rules.MAX_NAME_LENGTH + "  "
        assert len(padded) > schedule_rules.MAX_NAME_LENGTH
        assert schedule_rules.normalize_name(padded) == "x" * schedule_rules.MAX_NAME_LENGTH

    @pytest.mark.parametrize("raw", [1, 1.5, True, [], {}, ["a"]])
    def test_non_string_is_rejected(self, raw: object) -> None:
        with pytest.raises(TypeError):
            schedule_rules.normalize_name(raw)
