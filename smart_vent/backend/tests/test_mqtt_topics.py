"""Topic building and parsing (Issue #519)."""

from __future__ import annotations

import pytest

from backend.mqtt import topics

PREFIX = "plenum"


class TestParseCommand:
    def test_room_field_set(self) -> None:
        parsed = topics.parse_command(PREFIX, f"{PREFIX}/room/abc123/temp_offset/set")
        assert parsed is not None
        assert (parsed.device, parsed.ident, parsed.key, parsed.verb) == (
            "room",
            "abc123",
            "temp_offset",
            "set",
        )
        assert parsed.schedule_id is None

    def test_room_addressed_by_name(self) -> None:
        """The name tree parses identically; resolution happens later."""
        parsed = topics.parse_command(PREFIX, f"{PREFIX}/room/living_room/hold/set")
        assert parsed is not None and parsed.ident == "living_room"

    def test_room_clear(self) -> None:
        parsed = topics.parse_command(PREFIX, f"{PREFIX}/room/abc/deadband_override/clear")
        assert parsed is not None and parsed.verb == "clear"

    def test_schedule(self) -> None:
        parsed = topics.parse_command(PREFIX, f"{PREFIX}/room/abc/schedule/sched-1/set")
        assert parsed is not None
        assert (parsed.key, parsed.schedule_id) == ("schedule", "sched-1")

    def test_thermostat(self) -> None:
        parsed = topics.parse_command(PREFIX, f"{PREFIX}/thermostat/climate_upstairs/deadband/set")
        assert parsed is not None
        assert (parsed.device, parsed.ident, parsed.key) == (
            "thermostat",
            "climate_upstairs",
            "deadband",
        )

    def test_system_multi_segment_key(self) -> None:
        parsed = topics.parse_command(PREFIX, f"{PREFIX}/system/vacation_mode/return_at/set")
        assert parsed is not None
        assert (parsed.device, parsed.ident, parsed.key) == (
            "system",
            "",
            "vacation_mode/return_at",
        )

    def test_system_single_segment_key(self) -> None:
        parsed = topics.parse_command(PREFIX, f"{PREFIX}/system/enabled/set")
        assert parsed is not None and parsed.key == "enabled"

    @pytest.mark.parametrize(
        "topic",
        [
            # Our own publishes, echoed back by the wildcard subscription. If any
            # of these parsed as a command the bridge would act on its own state.
            f"{PREFIX}/room/abc/temp_offset/state",
            f"{PREFIX}/room/abc/temp_offset/set/result",
            f"{PREFIX}/room/abc/temp_offset/clear/result",
            f"{PREFIX}/status",
            # Malformed or foreign.
            f"{PREFIX}/room/abc/set",  # no control key
            f"{PREFIX}/room/set",  # no room
            f"{PREFIX}/system/set",  # no key
            f"{PREFIX}/nonsense/abc/key/set",
            "otherapp/room/abc/temp_offset/set",
            f"{PREFIX}/room/abc/schedule/s1/extra/set",
            f"{PREFIX}/room/abc/temp_offset/frobnicate",
        ],
    )
    def test_rejects_non_commands(self, topic: str) -> None:
        assert topics.parse_command(PREFIX, topic) is None

    def test_prefix_is_not_matched_as_a_substring(self) -> None:
        """`plenum_beta` must not answer on `plenum`'s tree, or the two
        installs #519 separates would share a topic space after all."""
        assert topics.parse_command("plenum", "plenum_beta/room/a/hold/set") is None
        assert topics.parse_command("plenum_beta", "plenum/room/a/hold/set") is None

    def test_round_trips_with_the_builders(self) -> None:
        topic = topics.command_topic(PREFIX, "room", "abc", "temp_offset", "set")
        parsed = topics.parse_command(PREFIX, topic)
        assert parsed is not None
        assert (parsed.device, parsed.ident, parsed.key) == ("room", "abc", "temp_offset")


class TestBuilders:
    def test_state_and_result(self) -> None:
        assert topics.state_topic(PREFIX, "room", "abc", "hold") == f"{PREFIX}/room/abc/hold/state"
        assert (
            topics.result_topic(PREFIX, "room", "abc", "hold", "set")
            == f"{PREFIX}/room/abc/hold/set/result"
        )

    def test_system_omits_the_ident_segment(self) -> None:
        assert (
            topics.state_topic(PREFIX, "system", "", "enabled") == f"{PREFIX}/system/enabled/state"
        )

    def test_schedule_key(self) -> None:
        assert topics.schedule_key("s1") == "schedule/s1"

    def test_wildcards_cover_every_device(self) -> None:
        wildcards = topics.command_wildcards(PREFIX)
        assert set(wildcards) == {
            f"{PREFIX}/room/#",
            f"{PREFIX}/thermostat/#",
            f"{PREFIX}/system/#",
        }
