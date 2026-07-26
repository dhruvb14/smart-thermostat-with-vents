"""Sanitisation and de-duplication of MQTT identifiers (Issue #519)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.mqtt.naming import dedupe_name, sanitize, sanitize_entity_id


class TestSanitize:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Office", "office"),
            ("OFFICE", "office"),
            ("  Office  ", "office"),
            ("Living Room", "living_room"),
            ("Kid's Room", "kid_s_room"),
            ("Upstairs-Office", "upstairs-office"),
            ("already_ok", "already_ok"),
            ("Room  2", "room_2"),
            ("héllo", "h_llo"),
            ("plenum_beta", "plenum_beta"),
        ],
    )
    def test_canonical_forms(self, raw: str, expected: str) -> None:
        assert sanitize(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "!!!", "---", "___", "!@#$%"])
    def test_nothing_usable_returns_empty(self, raw: str) -> None:
        """Callers decide what empty means; the rule itself never invents a name."""
        assert sanitize(raw) == ""

    def test_is_idempotent(self) -> None:
        """Sanitising twice must equal sanitising once, or a round-trip through
        a topic segment could keep changing the identifier."""
        for raw in ("Living Room", "Kid's Room", "  MIXED Case!  ", "a--b__c"):
            once = sanitize(raw)
            assert sanitize(once) == once

    def test_case_variants_collide(self) -> None:
        """The lossiness is the point: this is why uniqueness must be enforced
        on the sanitised form, not the raw string."""
        assert sanitize("Office") == sanitize("office") == sanitize("OFFICE")

    def test_entity_id_becomes_a_single_segment(self) -> None:
        """A dotted HA entity id must not introduce a topic level."""
        assert sanitize_entity_id("climate.upstairs") == "climate_upstairs"
        assert "/" not in sanitize_entity_id("climate.up/stairs")


class TestCrossLanguageParity:
    """The rule exists twice — here and in ``frontend/src/roomNames.ts``, which
    gives the Rooms form instant feedback. Both sides' tests read the same
    vectors, so a change to one implementation without the other fails CI.
    """

    CASES = Path(__file__).resolve().parents[2] / "frontend" / "src" / "roomNameCases.json"

    def test_the_shared_vector_file_exists(self) -> None:
        assert self.CASES.is_file(), (
            f"{self.CASES} is missing — it is the contract between the Python and "
            "TypeScript copies of the sanitisation rule."
        )

    def test_python_matches_every_shared_vector(self) -> None:
        cases = json.loads(self.CASES.read_text(encoding="utf-8"))["cases"]
        assert cases, "the shared vector file must not be empty"
        mismatches = [
            (case["raw"], case["sanitized"], sanitize(case["raw"]))
            for case in cases
            if sanitize(case["raw"]) != case["sanitized"]
        ]
        assert not mismatches, (
            "backend/mqtt/naming.py disagrees with roomNameCases.json on "
            f"{mismatches} — the frontend copy would accept names the API rejects."
        )


class TestDedupeName:
    def test_free_name_is_returned_unchanged(self) -> None:
        assert dedupe_name("Office", {"kitchen"}) == "Office"

    def test_appends_a_counter_on_collision(self) -> None:
        assert dedupe_name("Office", {"office"}) == "Office (2)"

    def test_skips_past_several_collisions(self) -> None:
        assert dedupe_name("Office", {"office", "office_2", "office_3"}) == "Office (4)"

    def test_result_is_actually_free(self) -> None:
        taken = {"office", "office_2"}
        assert sanitize(dedupe_name("Office", taken)) not in taken

    def test_is_deterministic(self) -> None:
        """Same inputs, same answer — so re-running the migration renames nothing."""
        taken = {"office"}
        assert dedupe_name("Office", taken) == dedupe_name("Office", taken)

    def test_does_not_mutate_the_taken_set(self) -> None:
        taken = {"office"}
        dedupe_name("Office", taken)
        assert taken == {"office"}
