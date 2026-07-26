"""The declarative table of every control MQTT exposes (Issue #519).

One entry per control, and everything else is derived from it: the HA Discovery
payload, the set of topics subscribed to, the command dispatched on a message,
and the retained state published back. Adding a control means adding a row here
— not touching four files that then drift apart.

Deliberately **not** here, per #519: the safety/equipment-protection cluster
(``min_cycle_runtime_min``, ``min_cycle_offtime_min``, ``cycle_timeout_hours``,
``min_open_vents_fraction``, ``max_vent_closed_min``,
``unavailable_abort_after_min``, ``overflow_during_min_runtime``,
``reconciliation_interval_min``). Those guard against real equipment damage and
MQTT's trust boundary is broker ACLs alone — weaker than the ``require_auth``
gate on the web UI and MCP. They stay Settings-page-only. Install-time hardware
facts (``total_vents_count``, ``has_bypass_damper``), free-text ``notes``,
display ``name``\\ s, and the how-Plenum-itself-runs settings are excluded too.

``ROOM_EXCLUDED_FIELDS`` and ``THERMOSTAT_EXCLUDED_FIELDS`` below pin that
decision down as data, so ``test_mqtt_registry.py`` can prove every writable
model field is either exposed or *consciously* excluded. A new field added to
``Room`` or ``ThermostatConfig`` fails that test until someone decides which it
is — the exclusion list is a decision record, not a denylist to pad.
"""

from __future__ import annotations

from dataclasses import dataclass

# Value codings. `kind` decides how a payload is parsed and how state is
# rendered; `temp` decides whether a number is a temperature and, if so, whether
# it is absolute (°F→°C subtracts 32) or a delta (it does not) — the #231
# distinction, applied to the display layer.
KIND_BOOL = "bool"
KIND_NUMBER = "number"
KIND_ENUM = "enum"
KIND_DATETIME = "datetime"
KIND_ACTION = "action"

TEMP_ABSOLUTE = "absolute"
TEMP_DELTA = "delta"

DEVICE_ROOM = "room"
DEVICE_THERMOSTAT = "thermostat"
DEVICE_SYSTEM = "system"


@dataclass(frozen=True)
class Control:
    """One exposed control.

    ``field`` set  → a plain write of that key to the device's REST resource
    (``PUT /api/rooms/{id}`` or ``PUT /api/thermostats/{entity_id}``).
    ``special`` set → a bespoke endpoint; see ``commands.build_request``.
    Exactly one of the two is always set.
    """

    key: str  # topic segment under the device
    entity: str  # HA Discovery component: switch|number|button|select|datetime
    name: str  # HA friendly name
    kind: str
    field: str | None = None
    special: str | None = None
    # None = not a temperature. Drives both the °F→display conversion on state
    # and the unit advertised to HA.
    temp: str | None = None
    # Nullable controls gain a `.../clear` button and accept an empty payload on
    # `.../set`, per #519's three-topic nullable pattern.
    nullable: bool = False
    options: tuple[str, ...] = ()
    min: float | None = None
    max: float | None = None
    step: float | None = None
    unit: str | None = None  # non-temperature unit_of_measurement (h, min)
    icon: str | None = None
    # Gains a `.../clear` topic without being nullable — the room hold, whose
    # clear removes the override rather than restoring an inherited value.
    clearable: bool = False
    # Which thermostat field a nullable room field falls back to when it is
    # null. Usually the same name (the Eco overrides), but not always:
    # `deadband_override` inherits `deadband` and `system_wide_temp` inherits
    # `default_temp`. Only used to render the effective value onto state.
    inherits_from: str | None = None

    def __post_init__(self) -> None:
        if (self.field is None) == (self.special is None):
            raise ValueError(f"control {self.key!r} must set exactly one of field/special")

    @property
    def inherit_field(self) -> str | None:
        """The parent field this control's value falls back to, if any."""
        if not self.nullable:
            return None
        return self.inherits_from or self.field

    @property
    def verbs(self) -> tuple[str, ...]:
        """Command verbs this control accepts, i.e. its ``.../<verb>`` topics."""
        if self.kind == KIND_ACTION:
            return ("clear",)
        if self.nullable or self.clearable:
            return ("set", "clear")
        return ("set",)

    @property
    def has_state(self) -> bool:
        """Whether the control publishes a retained state topic.

        Pure actions (HA ``button``\\ s) have nothing to report — firing one is
        the whole interaction.
        """
        return self.kind != KIND_ACTION


# ---------------------------------------------------------------------------
# Room device
# ---------------------------------------------------------------------------

ROOM_CONTROLS: tuple[Control, ...] = (
    Control(
        key="presence",
        entity="button",
        name="Clear Presence",
        kind=KIND_ACTION,
        special="presence_clear",
        icon="mdi:motion-sensor-off",
    ),
    # The hold mirrors REST exactly: no duration entity, so it takes the same
    # 2h default the API applies, and setting it fully replaces any existing
    # hold. A custom duration needs a raw command-topic payload.
    Control(
        key="hold",
        entity="number",
        name="Hold Temperature",
        kind=KIND_NUMBER,
        special="hold_set",
        temp=TEMP_ABSOLUTE,
        min=40,
        max=90,
        step=0.5,
        icon="mdi:thermometer-lines",
        clearable=True,
    ),
    Control(
        key="temp_offset",
        entity="number",
        name="Temperature Offset",
        kind=KIND_NUMBER,
        field="temp_offset",
        temp=TEMP_DELTA,
        min=-20,
        max=20,
        step=0.1,
    ),
    Control(
        key="presence_holdover_hours",
        entity="number",
        name="Presence Holdover",
        kind=KIND_NUMBER,
        field="presence_holdover_hours",
        min=0,
        max=8760,
        step=0.5,
        unit="h",
    ),
    Control(
        key="include_thermostat_sensor",
        entity="switch",
        name="Include Thermostat Sensor",
        kind=KIND_BOOL,
        field="include_thermostat_sensor",
    ),
    Control(
        key="ambient_suppression_enabled",
        entity="switch",
        name="Pre-cool / Pre-heat",
        kind=KIND_BOOL,
        field="ambient_suppression_enabled",
    ),
    Control(
        key="ambient_suppression_mode",
        entity="select",
        name="Pre-cool Mode",
        kind=KIND_ENUM,
        field="ambient_suppression_mode",
        options=("any_presence", "off_schedule_only"),
    ),
    Control(
        key="ambient_suppression_min_differential",
        entity="number",
        name="Pre-cool Minimum Difference",
        kind=KIND_NUMBER,
        field="ambient_suppression_min_differential",
        temp=TEMP_DELTA,
        min=0,
        max=50,
        step=0.1,
    ),
    Control(
        key="ambient_suppression_deadband",
        entity="number",
        name="Pre-cool Widened Deadband",
        kind=KIND_NUMBER,
        field="ambient_suppression_deadband",
        temp=TEMP_DELTA,
        min=0,
        max=20,
        step=0.1,
    ),
    Control(
        key="ambient_suppression_off_schedule_window_min",
        entity="number",
        name="Pre-cool Schedule Window",
        kind=KIND_NUMBER,
        field="ambient_suppression_off_schedule_window_min",
        min=0,
        max=1440,
        step=1,
        unit="min",
    ),
    # --- nullable (None = inherit / no override) -------------------------
    Control(
        key="system_wide_temp",
        entity="number",
        name="Presence Target",
        kind=KIND_NUMBER,
        field="system_wide_temp",
        temp=TEMP_ABSOLUTE,
        nullable=True,
        # Both are "the target presence activates this room to".
        inherits_from="default_temp",
        min=40,
        max=90,
        step=0.5,
    ),
    Control(
        key="deadband_override",
        entity="number",
        name="Deadband Override",
        kind=KIND_NUMBER,
        field="deadband_override",
        temp=TEMP_DELTA,
        nullable=True,
        # The room override replaces the thermostat's plain `deadband`.
        inherits_from="deadband",
        min=0,
        max=10,
        step=0.1,
    ),
    Control(
        key="eco_mode_enabled",
        entity="switch",
        name="Eco Mode",
        kind=KIND_BOOL,
        field="eco_mode_enabled",
        nullable=True,
    ),
)

# The seven per-field Eco overrides a room can set, mirrored from the
# thermostat's base values. Same names, same kinds — only nullability differs,
# so both tables are generated from one spec.
_ECO_NUMERIC_SPECS: tuple[tuple[str, str, str, float, float], ...] = (
    ("eco_cooling_outdoor_threshold", "Eco Cooling Outdoor Threshold", TEMP_ABSOLUTE, -50, 150),
    ("eco_cooling_full_drift_temp", "Eco Cooling Full Drift Temp", TEMP_ABSOLUTE, -50, 150),
    ("eco_cooling_max_drift", "Eco Cooling Max Drift", TEMP_DELTA, 0, 20),
    ("eco_heating_outdoor_threshold", "Eco Heating Outdoor Threshold", TEMP_ABSOLUTE, -50, 150),
    ("eco_heating_full_drift_temp", "Eco Heating Full Drift Temp", TEMP_ABSOLUTE, -50, 150),
    ("eco_heating_max_drift", "Eco Heating Max Drift", TEMP_DELTA, 0, 20),
    ("eco_hysteresis_band", "Eco Hysteresis Band", TEMP_DELTA, 0, 20),
)


def _eco_controls(nullable: bool) -> tuple[Control, ...]:
    return tuple(
        Control(
            key=key,
            entity="number",
            name=name,
            kind=KIND_NUMBER,
            field=key,
            temp=temp,
            nullable=nullable,
            min=lo,
            max=hi,
            step=0.1,
        )
        for key, name, temp, lo, hi in _ECO_NUMERIC_SPECS
    )


ROOM_CONTROLS = ROOM_CONTROLS + _eco_controls(nullable=True)

# Room-model fields deliberately NOT on MQTT. See the module docstring.
ROOM_EXCLUDED_FIELDS: frozenset[str] = frozenset(
    {
        "id",  # identity, not a control
        "name",  # display label; addressing only (see the name-alias tree)
        "thermostat_entity_id",  # re-parenting a room is a structural edit
        "notes",  # free text, no control value
    }
)


# ---------------------------------------------------------------------------
# Thermostat device
# ---------------------------------------------------------------------------

THERMOSTAT_CONTROLS: tuple[Control, ...] = (
    Control(
        key="min_setpoint",
        entity="number",
        name="Minimum Setpoint",
        kind=KIND_NUMBER,
        field="min_setpoint",
        temp=TEMP_ABSOLUTE,
        min=40,
        max=90,
        step=0.5,
    ),
    Control(
        key="max_setpoint",
        entity="number",
        name="Maximum Setpoint",
        kind=KIND_NUMBER,
        field="max_setpoint",
        temp=TEMP_ABSOLUTE,
        min=40,
        max=90,
        step=0.5,
    ),
    Control(
        key="deadband",
        entity="number",
        name="Deadband",
        kind=KIND_NUMBER,
        field="deadband",
        temp=TEMP_DELTA,
        min=0,
        max=10,
        step=0.1,
    ),
    Control(
        key="overshoot_delta",
        entity="number",
        name="Overshoot Delta",
        kind=KIND_NUMBER,
        field="overshoot_delta",
        temp=TEMP_DELTA,
        min=0,
        max=10,
        step=0.1,
    ),
    Control(
        key="vacation_hvac_mode",
        entity="select",
        name="Vacation HVAC Mode",
        kind=KIND_ENUM,
        field="vacation_hvac_mode",
        options=("range", "single"),
    ),
    Control(
        key="eco_mode_enabled",
        entity="switch",
        name="Eco Mode",
        kind=KIND_BOOL,
        field="eco_mode_enabled",
    ),
    # Eco Suspend follows the vacation-mode shape rather than #519's bare
    # switch: `POST /eco-suspend` requires a future `resume_at`, so a plain ON
    # has nothing to send. The datetime entity IS the suspend action; the
    # switch reports state and handles OFF, rejecting a bare ON through the
    # result channel. Same problem, same resolved answer as vacation mode.
    Control(
        key="eco_suspend_until",
        entity="datetime",
        name="Eco Suspend Until",
        kind=KIND_DATETIME,
        special="eco_suspend_set",
        icon="mdi:leaf-off",
    ),
    Control(
        key="eco_suspend",
        entity="switch",
        name="Eco Suspended",
        kind=KIND_BOOL,
        special="eco_suspend_toggle",
        icon="mdi:leaf-off",
    ),
    # --- nullable --------------------------------------------------------
    Control(
        key="default_temp",
        entity="number",
        name="Default Target",
        kind=KIND_NUMBER,
        field="default_temp",
        temp=TEMP_ABSOLUTE,
        nullable=True,
        min=40,
        max=90,
        step=0.5,
    ),
    Control(
        key="cooling_lockout_below_f",
        entity="number",
        name="Cooling Lockout Below",
        kind=KIND_NUMBER,
        field="cooling_lockout_below_f",
        temp=TEMP_ABSOLUTE,
        nullable=True,
        min=-50,
        max=90,
        step=1,
    ),
) + _eco_controls(nullable=False)

# ThermostatConfig fields deliberately NOT on MQTT. The first block is the
# safety/equipment-protection cluster #519 excludes on trust-boundary grounds.
THERMOSTAT_EXCLUDED_FIELDS: frozenset[str] = frozenset(
    {
        "min_cycle_runtime_min",
        "min_cycle_offtime_min",
        "cycle_timeout_hours",
        "min_open_vents_fraction",
        "max_vent_closed_min",
        "unavailable_abort_after_min",
        "overflow_during_min_runtime",
        "reconciliation_interval_min",
        # Install-time hardware facts about the house, not automation targets.
        "total_vents_count",
        "has_bypass_damper",
        # Identity / display label.
        "thermostat_entity_id",
        "name",
        # Read-only in API responses; written via the dedicated endpoints that
        # the eco_suspend* controls above already cover.
        "eco_suspend_until",
    }
)


# ---------------------------------------------------------------------------
# System device
# ---------------------------------------------------------------------------

SYSTEM_CONTROLS: tuple[Control, ...] = (
    # Publishing a future datetime here IS the enable action, matching
    # `POST /api/settings/vacation-mode`, which refuses to enable without one.
    Control(
        key="vacation_mode/return_at",
        entity="datetime",
        name="Vacation Return At",
        kind=KIND_DATETIME,
        special="vacation_return_at",
        icon="mdi:calendar-clock",
    ),
    Control(
        key="vacation_mode",
        entity="switch",
        name="Vacation Mode",
        kind=KIND_BOOL,
        special="vacation_toggle",
        icon="mdi:bag-suitcase",
    ),
    Control(
        key="enabled",
        entity="switch",
        name="System Enabled",
        kind=KIND_BOOL,
        special="system_enabled",
        icon="mdi:power",
    ),
)

# The per-schedule switch is not in a static table — schedules are created and
# deleted at runtime, so its controls are generated per schedule id.
SCHEDULE_CONTROL = Control(
    key="enabled",
    entity="switch",
    name="Enabled",
    kind=KIND_BOOL,
    special="schedule_enabled",
)


CONTROLS_BY_DEVICE: dict[str, tuple[Control, ...]] = {
    DEVICE_ROOM: ROOM_CONTROLS,
    DEVICE_THERMOSTAT: THERMOSTAT_CONTROLS,
    DEVICE_SYSTEM: SYSTEM_CONTROLS,
}


def control_for(device: str, key: str) -> Control | None:
    """Look up a control by device and topic key."""
    for control in CONTROLS_BY_DEVICE.get(device, ()):
        if control.key == key:
            return control
    return None
