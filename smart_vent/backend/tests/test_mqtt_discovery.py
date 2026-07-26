"""HA MQTT Discovery payload generation (Issue #519)."""

from __future__ import annotations

from backend.mqtt import discovery
from backend.mqtt.registry import (
    DEVICE_ROOM,
    DEVICE_SYSTEM,
    DEVICE_THERMOSTAT,
    ROOM_CONTROLS,
    SYSTEM_CONTROLS,
    THERMOSTAT_CONTROLS,
    Control,
    control_for,
)

PREFIX = "plenum"
DPREFIX = "homeassistant"
ROOM_ID = "room-guid-1"


def _control(device: str, key: str) -> Control:
    """Look a control up, failing loudly if the registry drops it."""
    found = control_for(device, key)
    assert found is not None, f"{device}/{key} missing from the registry"
    return found


def _build(
    control: Control, *, device=DEVICE_ROOM, ident=ROOM_ID, topic_ident=None, unit="F", **kw
):
    info = discovery.device_block(PREFIX, device, ident or "plenum", "Plenum Test")
    return discovery.build_entities(
        control,
        prefix=PREFIX,
        discovery_prefix=DPREFIX,
        device=device,
        ident=ident,
        topic_ident=ident if topic_ident is None else topic_ident,
        device_info=info,
        unit=unit,
        **kw,
    )


class TestConfigTopics:
    def test_config_lives_under_has_prefix_not_ours(self) -> None:
        """#519: discovery configs go under HA's discovery prefix; only the
        state/command topics point back at our tree."""
        entity = _build(_control(DEVICE_ROOM, "temp_offset"))[0]
        assert entity.topic.startswith(f"{DPREFIX}/number/")
        assert entity.topic.endswith("/config")
        assert entity.payload["command_topic"].startswith(f"{PREFIX}/")

    def test_component_matches_the_control_kind(self) -> None:
        cases = {
            "temp_offset": "number",
            "include_thermostat_sensor": "switch",
            "ambient_suppression_mode": "select",
            "presence": "button",
        }
        for key, component in cases.items():
            entity = _build(_control(DEVICE_ROOM, key))[0]
            assert f"/{component}/" in entity.topic, key


class TestIdentityIsIdBased:
    def test_unique_id_derives_from_the_room_guid(self) -> None:
        entity = _build(_control(DEVICE_ROOM, "temp_offset"))[0]
        assert "room-guid-1" in entity.payload["unique_id"].replace("_", "-")

    def test_renaming_a_room_changes_no_unique_id(self) -> None:
        """The whole reason discovery stays id-based: a rename must not orphan
        HA's entity registry or mint a duplicate entity."""
        control = _control(DEVICE_ROOM, "temp_offset")
        before = _build(control)[0]
        info = discovery.device_block(PREFIX, DEVICE_ROOM, ROOM_ID, "Plenum Totally New Name")
        after = discovery.build_entities(
            control,
            prefix=PREFIX,
            discovery_prefix=DPREFIX,
            device=DEVICE_ROOM,
            ident=ROOM_ID,
            topic_ident=ROOM_ID,
            device_info=info,
            unit="F",
        )[0]
        assert before.payload["unique_id"] == after.payload["unique_id"]
        assert before.topic == after.topic
        assert before.payload["device"]["identifiers"] == after.payload["device"]["identifiers"]

    def test_discovery_topics_never_use_the_name_alias(self) -> None:
        entity = _build(_control(DEVICE_ROOM, "hold"))[0]
        assert ROOM_ID in entity.payload["command_topic"]
        assert entity.payload["state_topic"].startswith(f"{PREFIX}/room/{ROOM_ID}/")

    def test_unique_ids_are_unique_across_the_whole_tree(self) -> None:
        seen = set()
        for control in ROOM_CONTROLS:
            for entity in _build(control):
                assert entity.payload["unique_id"] not in seen
                seen.add(entity.payload["unique_id"])
        for control in THERMOSTAT_CONTROLS:
            for entity in _build(
                control,
                device=DEVICE_THERMOSTAT,
                ident="climate.upstairs",
                topic_ident="climate_upstairs",
            ):
                assert entity.payload["unique_id"] not in seen
                seen.add(entity.payload["unique_id"])
        for control in SYSTEM_CONTROLS:
            for entity in _build(control, device=DEVICE_SYSTEM, ident=""):
                assert entity.payload["unique_id"] not in seen
                seen.add(entity.payload["unique_id"])

    def test_thermostat_entity_id_is_sanitised_into_one_segment(self) -> None:
        entity = _build(
            _control(DEVICE_THERMOSTAT, "deadband"),
            device=DEVICE_THERMOSTAT,
            ident="climate.upstairs",
            topic_ident="climate_upstairs",
        )[0]
        assert (
            f"{PREFIX}/thermostat/climate_upstairs/deadband/set"
            == (entity.payload["command_topic"])
        )


class TestTemperatureUnits:
    def test_absolute_temperature_advertises_the_active_unit(self) -> None:
        for unit, label in (("F", "°F"), ("C", "°C")):
            entity = _build(_control(DEVICE_ROOM, "system_wide_temp"), unit=unit)[0]
            assert entity.payload["unit_of_measurement"] == label

    def test_absolute_gets_a_temperature_device_class(self) -> None:
        entity = _build(_control(DEVICE_ROOM, "system_wide_temp"))[0]
        assert entity.payload["device_class"] == "temperature"

    def test_delta_carries_the_unit_but_not_the_device_class(self) -> None:
        """A deadband is a span, not a reading. Tagging it `temperature` would
        have HA re-convert it against its own unit system and mangle it."""
        entity = _build(_control(DEVICE_ROOM, "temp_offset"))[0]
        assert entity.payload["unit_of_measurement"] == "°F"
        assert "device_class" not in entity.payload

    def test_non_temperature_units_pass_through(self) -> None:
        entity = _build(_control(DEVICE_ROOM, "presence_holdover_hours"))[0]
        assert entity.payload["unit_of_measurement"] == "h"
        assert "device_class" not in entity.payload


class TestClearButtons:
    def test_nullable_control_gains_an_inherit_button(self) -> None:
        entities = _build(_control(DEVICE_ROOM, "deadband_override"))
        assert len(entities) == 2
        button = entities[1]
        assert "/button/" in button.topic
        assert button.payload["command_topic"].endswith("/deadband_override/clear")
        assert "Inherit" in button.payload["name"]

    def test_hold_gains_a_clear_button(self) -> None:
        entities = _build(_control(DEVICE_ROOM, "hold"))
        assert len(entities) == 2
        assert "Clear" in entities[1].payload["name"]

    def test_plain_control_has_no_extra_button(self) -> None:
        assert len(_build(_control(DEVICE_ROOM, "temp_offset"))) == 1

    def test_button_control_has_no_state_topic(self) -> None:
        entity = _build(_control(DEVICE_ROOM, "presence"))[0]
        assert "state_topic" not in entity.payload
        assert entity.payload["payload_press"] == "PRESS"


class TestAvailability:
    def test_every_entity_is_gated_on_the_lwt_topic(self) -> None:
        """Without this, a dead add-on leaves stale retained values looking live."""
        for control in ROOM_CONTROLS:
            for entity in _build(control):
                assert entity.payload["availability_topic"] == f"{PREFIX}/status"
                assert entity.payload["payload_not_available"] == "offline"


class TestDeviceGrouping:
    def test_rooms_and_thermostats_are_separate_devices(self) -> None:
        room = discovery.device_block(PREFIX, DEVICE_ROOM, ROOM_ID, "Plenum Office")
        thermostat = discovery.device_block(
            PREFIX, DEVICE_THERMOSTAT, "climate.upstairs", "Plenum Upstairs"
        )
        assert room["identifiers"] != thermostat["identifiers"]

    def test_room_and_thermostat_hang_off_the_system_device(self) -> None:
        room = discovery.device_block(PREFIX, DEVICE_ROOM, ROOM_ID, "Plenum Office")
        assert room["via_device"] == f"{PREFIX}_system"

    def test_system_device_has_no_parent(self) -> None:
        system = discovery.device_block(PREFIX, DEVICE_SYSTEM, "plenum", "Plenum System")
        assert "via_device" not in system

    def test_prefix_separates_two_installs(self) -> None:
        """Stable and beta on one broker must not share a device or entity."""
        stable = discovery.device_block("plenum", DEVICE_ROOM, ROOM_ID, "Plenum Office")
        beta = discovery.device_block("plenum_beta", DEVICE_ROOM, ROOM_ID, "Plenum Office")
        assert stable["identifiers"] != beta["identifiers"]


class TestScheduleEntities:
    def test_named_schedule_uses_its_label(self) -> None:
        from backend.mqtt.registry import SCHEDULE_CONTROL
        from backend.mqtt.topics import schedule_key

        entity = _build(
            SCHEDULE_CONTROL,
            topic_key=schedule_key("sched-1"),
            name_override="Schedule: Night setback",
        )[0]
        assert entity.payload["name"] == "Schedule: Night setback"
        assert entity.payload["command_topic"].endswith("/schedule/sched-1/set")


def test_removal_topics_are_the_config_topics() -> None:
    entities = _build(_control(DEVICE_ROOM, "deadband_override"))
    assert discovery.removal_topics(entities) == [e.topic for e in entities]
