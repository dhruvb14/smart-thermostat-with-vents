import { describe, it, expect, vi, beforeEach } from "vitest"; // trigger CI
import { ecoThermostatDefaults, ecoRoomDefaults } from "../testFixtures";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import Rooms from "./Rooms";
import * as api from "../api";
import { SystemContext, UnitContext, buildUnitContext } from "../contexts";

vi.mock("../api");

const mockThermostats: api.ThermostatConfig[] = [
  {
    thermostat_entity_id: "climate.test",
    name: "Test Thermostat",
    default_temp: 72,
    min_setpoint: 60,
    max_setpoint: 80,
    deadband: 0.5,
    max_vent_closed_min: 60,
    total_vents_count: null,

    has_bypass_damper: false,

    min_open_vents_fraction: 0.333,
    overshoot_delta: 0.5,
    cycle_timeout_hours: 2,
    reconciliation_interval_min: 5,
    vacation_hvac_mode: "single" as const,
    min_cycle_runtime_min: 0,
    min_cycle_offtime_min: 0,
    cooling_lockout_below_f: null,
    overflow_during_min_runtime: true,
    unavailable_abort_after_min: 5,
    ...ecoThermostatDefaults,
  },
];

const mockRooms: api.Room[] = [
  {
    id: "room-1",
    name: "Living Room",
    thermostat_entity_id: "climate.test",
    include_thermostat_sensor: false,
    presence_holdover_hours: 2,
    temp_offset: 0,
    deadband_override: null,
    system_wide_temp: 72,
    notes: "",
    ambient_suppression_enabled: false,
    ambient_suppression_mode: "any_presence",
    ambient_suppression_min_differential: 5,
    ambient_suppression_deadband: 2,
    ambient_suppression_off_schedule_window_min: 60,
    ...ecoRoomDefaults,
    sensors: [{ id: "s1", room_id: "room-1", entity_id: "sensor.temp" }],
    vents: [{ id: "v1", room_id: "room-1", entity_id: "cover.vent", control_method: "open_close" }],
    presence_sensors: [],
  },
];

const mockSystem = {
  enabled: true,
  toggle: async () => {},
};

describe("Rooms Page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getRooms).mockResolvedValue(mockRooms);
    vi.mocked(api.getThermostats).mockResolvedValue(mockThermostats);
    vi.mocked(api.getRoom).mockImplementation((id: string) =>
      Promise.resolve(mockRooms.find((r) => r.id === id) as api.Room)
    );
    vi.mocked(api.getRoomActiveStatuses).mockResolvedValue({
      "room-1": {
        room_id: "room-1",
        source: "idle",
        target_temp: null,
        ends_in_seconds: null,
        presence_holdover_active: false,
        presence_suppressed: false,
        next_schedule_in_seconds: null,
        next_schedule_target: null,
        next_schedule_label: null,
      },
    });
    vi.mocked(api.getEntityStates).mockResolvedValue({
      "sensor.temp": { state: "72.5", numeric: 72.5, unit: "°F", attributes: {} },
      "cover.vent": {
        state: "open",
        numeric: null,
        unit: "",
        attributes: { current_position: 100 },
      },
    });
    vi.mocked(api.getHAEntities).mockResolvedValue([
      { entity_id: "sensor.another_temp", friendly_name: "Another Temp", state: "" },
      { entity_id: "cover.another_vent", friendly_name: "Another Vent", state: "" },
      { entity_id: "binary_sensor.motion", friendly_name: "Motion", state: "" },
    ]);
    // Default: an outside sensor is configured, so pre-cool/pre-heat is enabled.
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: "sensor.outdoor",
      current_value: 80,
    });
  });

  it("renders the rooms list", async () => {
    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    expect(await screen.findByText("Living Room")).toBeInTheDocument();
    expect(screen.getByText(/Test Thermostat/)).toBeInTheDocument();
    expect(await screen.findByText("72.5°F")).toBeInTheDocument();
  });

  it("shows validation error for empty room name", async () => {
    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    const addBtn = await screen.findByText("+ Add room");
    fireEvent.click(addBtn);

    const createBtn = await screen.findByRole("button", { name: /Create room/i });
    fireEvent.click(createBtn);

    expect(await screen.findByText("Room name is required")).toBeInTheDocument();
  });

  it("shows validation error for negative holdover", async () => {
    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    const addBtn = await screen.findByText("+ Add room");
    fireEvent.click(addBtn);

    await screen.findByText("New Room", { selector: ".page-title" });

    const nameInput = screen.getByLabelText(/Room name/i);
    fireEvent.change(nameInput, { target: { value: "New Room" } });

    const thermoSelect = screen.getByRole("combobox", { name: /Thermostat/i });
    fireEvent.change(thermoSelect, { target: { value: "climate.test" } });

    const holdoverInput = screen.getByLabelText(/Presence holdover/i);
    fireEvent.change(holdoverInput, { target: { value: "-1" } });

    const createBtn = screen.getByRole("button", { name: /Create room/i });
    fireEvent.click(createBtn);

    expect(await screen.findByText("Presence holdover must be >= 0")).toBeInTheDocument();
  });

  it("successfully creates a room", async () => {
    vi.mocked(api.createRoom).mockResolvedValue({
      id: "room-2",
      name: "New Room",
      thermostat_entity_id: "climate.test",
    } as api.Room);

    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    const addBtn = await screen.findByText("+ Add room");
    fireEvent.click(addBtn);

    await screen.findByText("New Room", { selector: ".page-title" });

    const nameInput = screen.getByLabelText(/Room name/i);
    fireEvent.change(nameInput, { target: { value: "New Room" } });

    const thermoSelect = screen.getByRole("combobox", { name: /Thermostat/i });
    fireEvent.change(thermoSelect, { target: { value: "climate.test" } });

    const createBtn = screen.getByRole("button", { name: /Create room/i });
    fireEvent.click(createBtn);

    await waitFor(() => {
      expect(api.createRoom).toHaveBeenCalledWith(
        expect.objectContaining({
          name: "New Room",
          thermostat_entity_id: "climate.test",
        })
      );
    });
  });

  it("handles room editing", async () => {
    vi.mocked(api.updateRoom).mockResolvedValue({ ...mockRooms[0], name: "Edited Name" });

    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    const editBtn = await screen.findByRole("button", { name: /Settings/i });
    fireEvent.click(editBtn);

    const nameInput = screen.getByLabelText(/Room name/i);
    fireEvent.change(nameInput, { target: { value: "Edited Name" } });

    const saveBtn = screen.getByRole("button", { name: /Save changes/i });
    fireEvent.click(saveBtn);

    await waitFor(() => {
      expect(api.updateRoom).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({
          name: "Edited Name",
        })
      );
    });
  });

  it("handles room deletion", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.mocked(api.deleteRoom).mockResolvedValue({ deleted: "room-1" });

    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    const deleteBtn = await screen.findByRole("button", { name: /Delete/i });
    fireEvent.click(deleteBtn);

    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => {
      expect(api.deleteRoom).toHaveBeenCalledWith("room-1");
    });
  });

  it("navigates to configure view and manages entities", async () => {
    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    const configBtn = await screen.findByRole("button", { name: /Configure sensors/i });
    fireEvent.click(configBtn);

    expect(await screen.findByText("Temperature Sensors")).toBeInTheDocument();

    // Add a sensor
    const sensorPicker = screen.getByPlaceholderText(/Search temperature sensors/i);
    fireEvent.focus(sensorPicker);
    fireEvent.change(sensorPicker, { target: { value: "another" } });
    const sensorOption = await screen.findByText("Another Temp");
    fireEvent.mouseDown(sensorOption);

    await waitFor(() => {
      expect(api.addSensor).toHaveBeenCalledWith("room-1", "sensor.another_temp");
    });

    // Remove a sensor
    const sensorSection = screen.getByText("Temperature Sensors").closest("div")?.parentElement;
    const removeSensorBtn = within(sensorSection!).getAllByTitle("Remove")[0];
    fireEvent.click(removeSensorBtn);
    await waitFor(() => {
      expect(api.removeSensor).toHaveBeenCalledWith("room-1", "sensor.temp");
    });

    // Test vent
    const testOpenBtn = screen.getByText("Test open");
    fireEvent.click(testOpenBtn);
    expect(api.testVent).toHaveBeenCalledWith("cover.vent", "open_close", "open");

    // Remove vent
    const ventSection = screen.getByText("Vents").closest("div")?.parentElement;
    const removeVentBtn = within(ventSection!).getAllByTitle("Remove")[0];
    fireEvent.click(removeVentBtn);
    await waitFor(() => {
      expect(api.removeVent).toHaveBeenCalledWith("room-1", "cover.vent");
    });
  });

  it("changes a vent's control method in the configure view", async () => {
    vi.mocked(api.updateVentControlMethod).mockResolvedValue({
      updated: true,
      control_method: "set_position",
    });
    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    fireEvent.click(await screen.findByRole("button", { name: /Configure sensors/i }));
    await screen.findByText("Vents");

    const methodSelect = screen.getByDisplayValue(/Open \/ close/i);
    fireEvent.change(methodSelect, { target: { value: "set_position" } });

    await waitFor(() => {
      expect(api.updateVentControlMethod).toHaveBeenCalledWith(
        "room-1",
        "cover.vent",
        "set_position"
      );
    });
  });

  it("reports a failure when changing the vent control method errors", async () => {
    vi.mocked(api.updateVentControlMethod).mockRejectedValue(new Error("nope"));
    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    fireEvent.click(await screen.findByRole("button", { name: /Configure sensors/i }));
    await screen.findByText("Vents");

    fireEvent.change(screen.getByDisplayValue(/Open \/ close/i), {
      target: { value: "toggle" },
    });

    expect(await screen.findByText(/Save failed: nope/i)).toBeInTheDocument();
  });

  it("runs the vent close test in the configure view", async () => {
    vi.mocked(api.testVent).mockResolvedValue({ ok: true });
    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    fireEvent.click(await screen.findByRole("button", { name: /Configure sensors/i }));
    fireEvent.click(await screen.findByText("Test close"));

    expect(api.testVent).toHaveBeenCalledWith("cover.vent", "open_close", "close");
    expect(await screen.findByText(/Close command accepted/i)).toBeInTheDocument();
  });

  it("adds a presence sensor from the configure view", async () => {
    vi.mocked(api.addPresence).mockResolvedValue({
      id: "p1",
      room_id: "room-1",
      entity_id: "binary_sensor.motion",
    });
    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    fireEvent.click(await screen.findByRole("button", { name: /Configure sensors/i }));
    const picker = await screen.findByPlaceholderText(/Search motion\/presence sensors/i);
    fireEvent.focus(picker);
    fireEvent.change(picker, { target: { value: "Motion" } });
    fireEvent.mouseDown(await screen.findByText("Motion"));

    await waitFor(() => {
      expect(api.addPresence).toHaveBeenCalledWith("room-1", "binary_sensor.motion");
    });
  });

  it("edits room settings from the configure view, exercising every field", async () => {
    vi.mocked(api.updateRoom).mockResolvedValue(mockRooms[0]);
    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    fireEvent.click(await screen.findByRole("button", { name: /Configure sensors/i }));
    fireEvent.click(await screen.findByRole("button", { name: /Edit settings/i }));

    fireEvent.change(screen.getByLabelText(/Room name/i), { target: { value: "Renamed" } });
    fireEvent.change(screen.getByLabelText(/Presence holdover/i), { target: { value: "3" } });
    fireEvent.click(screen.getByLabelText(/Include thermostat's built-in sensor/i));
    fireEvent.change(screen.getByLabelText(/Temperature offset/i), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText(/Deadband override/i), { target: { value: "1.5" } });
    fireEvent.change(screen.getByLabelText(/Notes/i), { target: { value: "hello" } });

    fireEvent.click(screen.getByRole("button", { name: /Save changes/i }));

    await waitFor(() => {
      expect(api.updateRoom).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({
          name: "Renamed",
          presence_holdover_hours: 3,
          include_thermostat_sensor: true,
          deadband_override: 1.5,
          notes: "hello",
        })
      );
    });
  });

  it("sends null deadband_override when the field is left blank (inherit)", async () => {
    vi.mocked(api.updateRoom).mockResolvedValue(mockRooms[0]);
    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    fireEvent.click(await screen.findByRole("button", { name: /Configure sensors/i }));
    fireEvent.click(await screen.findByRole("button", { name: /Edit settings/i }));

    // mockRooms[0] has no deadband_override → the field renders blank and the
    // payload carries null so the room keeps inheriting the thermostat deadband.
    fireEvent.click(screen.getByRole("button", { name: /Save changes/i }));

    await waitFor(() => {
      expect(api.updateRoom).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({ deadband_override: null })
      );
    });
  });

  it("clears an existing override back to null when the field is emptied", async () => {
    // The user's "changed my mind" flow: a room that already HAS an override,
    // opened on the settings page (field pre-populated), cleared, saved → null.
    const roomWithOverride = { ...mockRooms[0], deadband_override: 1.5 };
    vi.mocked(api.getRooms).mockResolvedValue([roomWithOverride]);
    vi.mocked(api.getRoom).mockResolvedValue(roomWithOverride);
    vi.mocked(api.updateRoom).mockResolvedValue(roomWithOverride);
    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    fireEvent.click(await screen.findByRole("button", { name: /Configure sensors/i }));
    fireEvent.click(await screen.findByRole("button", { name: /Edit settings/i }));

    // Field initialises with the stored override (1.5°F → "1.5" in °F mode).
    const overrideInput = screen.getByLabelText(/Deadband override/i) as HTMLInputElement;
    expect(overrideInput.value).toBe("1.5");

    // Clear it and save — no validation error, payload carries null.
    fireEvent.change(overrideInput, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: /Save changes/i }));

    await waitFor(() => {
      expect(api.updateRoom).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({ deadband_override: null })
      );
    });
  });

  it("returns to the room list with the back button", async () => {
    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    fireEvent.click(await screen.findByRole("button", { name: /Configure sensors/i }));
    fireEvent.click(await screen.findByRole("button", { name: /All rooms/i }));

    // Back on the list view: the "+ Add room" button is present again
    expect(await screen.findByText("+ Add room")).toBeInTheDocument();
  });

  it("closes the settings page via Cancel without saving", async () => {
    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    fireEvent.click(await screen.findByRole("button", { name: /Settings/i }));
    fireEvent.click(await screen.findByRole("button", { name: /Cancel/i }));

    await waitFor(() => {
      expect(screen.queryByLabelText(/Room name/i)).not.toBeInTheDocument();
    });
    expect(api.updateRoom).not.toHaveBeenCalled();
  });
});

describe("Rooms Page — Celsius mode", () => {
  const renderInCelsius = () =>
    render(
      <UnitContext.Provider value={buildUnitContext("C")}>
        <SystemContext.Provider value={mockSystem}>
          <Rooms />
        </SystemContext.Provider>
      </UnitContext.Provider>
    );

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getRooms).mockResolvedValue(mockRooms);
    vi.mocked(api.getThermostats).mockResolvedValue(mockThermostats);
    vi.mocked(api.getRoom).mockImplementation((id: string) =>
      Promise.resolve(mockRooms.find((r) => r.id === id) as api.Room)
    );
    vi.mocked(api.getRoomActiveStatuses).mockResolvedValue({
      "room-1": {
        room_id: "room-1",
        source: "idle",
        target_temp: null,
        ends_in_seconds: null,
        presence_holdover_active: false,
        presence_suppressed: false,
        next_schedule_in_seconds: null,
        next_schedule_target: null,
        next_schedule_label: null,
      },
    });
    vi.mocked(api.getEntityStates).mockResolvedValue({
      "sensor.temp": { state: "72.5", numeric: 72.5, unit: "°F", attributes: {} },
      "cover.vent": {
        state: "open",
        numeric: null,
        unit: "",
        attributes: { current_position: 100 },
      },
    });
    vi.mocked(api.getHAEntities).mockResolvedValue([]);
  });

  it("displays live sensor avgTemp converted to °C on room card", async () => {
    // sensor.temp numeric=72.5°F → fmtTemp(72.5) in °C = "22.5°C"
    renderInCelsius();
    expect(await screen.findByText("22.5°C")).toBeInTheDocument();
  });

  it("shows presence temp and offset labels in °C on the settings page", async () => {
    renderInCelsius();
    const editBtn = await screen.findByRole("button", { name: /Settings/i });
    fireEvent.click(editBtn);

    expect(screen.getByText(/Presence-triggered temperature \(°C\)/i)).toBeInTheDocument();
    expect(screen.getByText(/Temperature offset \(°C\)/i)).toBeInTheDocument();
  });

  it("pre-populates system_wide_temp input in °C on the settings page", async () => {
    // mockRooms[0].system_wide_temp = 72°F → toDisplay(72) = 22.2°C
    renderInCelsius();
    const editBtn = await screen.findByRole("button", { name: /Settings/i });
    fireEvent.click(editBtn);

    const sysTempInput = screen.getByLabelText(
      /Presence-triggered temperature/i
    ) as HTMLInputElement;
    expect(parseFloat(sysTempInput.value)).toBeCloseTo(22.2, 1);
  });

  it("sends the user's raw °C system_wide_temp when updating room (#231)", async () => {
    vi.mocked(api.updateRoom).mockResolvedValue({ ...mockRooms[0], system_wide_temp: 71.6 });

    renderInCelsius();
    const editBtn = await screen.findByRole("button", { name: /Settings/i });
    fireEvent.click(editBtn);

    const sysTempInput = screen.getByLabelText(/Presence-triggered temperature/i);
    // Frontend sends display value as-is; backend's _to_f converts °C → °F.
    fireEvent.change(sysTempInput, { target: { value: "22" } });

    fireEvent.click(screen.getByRole("button", { name: /Save changes/i }));

    await waitFor(() => {
      expect(api.updateRoom).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({ system_wide_temp: 22 })
      );
    });
  });

  it("sends the user's raw °C deadband_override when updating room (#231)", async () => {
    vi.mocked(api.updateRoom).mockResolvedValue(mockRooms[0]);

    renderInCelsius();
    const editBtn = await screen.findByRole("button", { name: /Settings/i });
    fireEvent.click(editBtn);

    // deadband_override is a delta: the frontend sends the display value as-is
    // and the backend's _delta_to_f converts °C → °F. Asserting the raw value
    // guards against the #231 double-conversion.
    fireEvent.change(screen.getByLabelText(/Deadband override/i), { target: { value: "1" } });

    fireEvent.click(screen.getByRole("button", { name: /Save changes/i }));

    await waitFor(() => {
      expect(api.updateRoom).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({ deadband_override: 1 })
      );
    });
  });

  it("sends the user's raw °C eco override fields when updating room (#231, #417)", async () => {
    // eco_cooling_outdoor_threshold is absolute (would wrongly become 86°F if
    // pre-converted); eco_cooling_max_drift is a delta (would wrongly become
    // 3.6°F). Both must arrive as the raw °C number the user typed — the
    // backend's _to_f / _delta_to_f do the conversion at the write boundary.
    vi.mocked(api.updateRoom).mockResolvedValue(mockRooms[0]);

    renderInCelsius();
    const editBtn = await screen.findByRole("button", { name: /Settings/i });
    fireEvent.click(editBtn);

    fireEvent.change(screen.getByLabelText(/Cooling.*outdoor threshold/i), {
      target: { value: "30" },
    });
    fireEvent.change(screen.getByLabelText(/Cooling.*max drift/i), {
      target: { value: "2" },
    });

    fireEvent.click(screen.getByRole("button", { name: /Save changes/i }));

    await waitFor(() => {
      expect(api.updateRoom).toHaveBeenCalled();
    });
    const [, payload] = vi.mocked(api.updateRoom).mock.calls[0];
    expect(payload.eco_cooling_outdoor_threshold).toBe(30);
    expect(payload.eco_cooling_outdoor_threshold).not.toBe(86);
    expect(payload.eco_cooling_max_drift).toBe(2);
    expect(payload.eco_cooling_max_drift).not.toBe(3.6);
  });

  it("sends the user's raw °C ambient-suppression fields when updating room (#231, #417)", async () => {
    // ambient_suppression_min_differential and ambient_suppression_deadband
    // are both deltas — a pre-converted °F payload would send 3.6 instead of
    // the raw 2 the user typed. The checkbox needs an outside sensor to enable.
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: "sensor.outdoor",
      current_value: 80,
    });
    vi.mocked(api.updateRoom).mockResolvedValue(mockRooms[0]);

    renderInCelsius();
    const editBtn = await screen.findByRole("button", { name: /Settings/i });
    fireEvent.click(editBtn);

    const ambientCheckbox = (await screen.findByLabelText(
      /Skip presence heating\/cooling/i
    )) as HTMLInputElement;
    await waitFor(() => expect(ambientCheckbox).not.toBeDisabled());
    fireEvent.click(ambientCheckbox);

    fireEvent.change(screen.getByLabelText(/Minimum outside difference \(°C\)/i), {
      target: { value: "2" },
    });
    fireEvent.change(screen.getByLabelText(/Widened deadband \(°C\)/i), {
      target: { value: "2" },
    });

    fireEvent.click(screen.getByRole("button", { name: /Save changes/i }));

    await waitFor(() => {
      expect(api.updateRoom).toHaveBeenCalled();
    });
    const [, payload] = vi.mocked(api.updateRoom).mock.calls[0];
    expect(payload.ambient_suppression_min_differential).toBe(2);
    expect(payload.ambient_suppression_min_differential).not.toBe(3.6);
    expect(payload.ambient_suppression_deadband).toBe(2);
    expect(payload.ambient_suppression_deadband).not.toBe(3.6);
  });
});

describe("Rooms Page — Clear presence button", () => {
  const activePresenceStatus: api.RoomActiveStatus = {
    room_id: "room-1",
    source: "presence",
    target_temp: 72,
    ends_in_seconds: 3600,
    presence_holdover_active: true,
    presence_suppressed: false,
    next_schedule_in_seconds: null,
    next_schedule_target: null,
    next_schedule_label: null,
  };

  const idleStatus: api.RoomActiveStatus = {
    room_id: "room-1",
    source: "idle",
    target_temp: null,
    ends_in_seconds: null,
    presence_holdover_active: false,
    presence_suppressed: false,
    next_schedule_in_seconds: null,
    next_schedule_target: null,
    next_schedule_label: null,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getRooms).mockResolvedValue(mockRooms);
    vi.mocked(api.getThermostats).mockResolvedValue(mockThermostats);
    vi.mocked(api.getRoom).mockImplementation((id: string) =>
      Promise.resolve(mockRooms.find((r) => r.id === id) as api.Room)
    );
    vi.mocked(api.getEntityStates).mockResolvedValue({});
    vi.mocked(api.getHAEntities).mockResolvedValue([]);
  });

  it("shows Clear presence button when presence_holdover_active is true", async () => {
    vi.mocked(api.getRoomActiveStatuses).mockResolvedValue({ "room-1": activePresenceStatus });

    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    expect(await screen.findByRole("button", { name: /Clear presence/i })).toBeInTheDocument();
  });

  it("does not show Clear presence button when presence_holdover_active is false", async () => {
    vi.mocked(api.getRoomActiveStatuses).mockResolvedValue({ "room-1": idleStatus });

    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    await screen.findByText("Living Room");
    expect(screen.queryByRole("button", { name: /Clear presence/i })).not.toBeInTheDocument();
  });

  it("explains a cleared-but-still-occupied room via the suppression hint (#439)", async () => {
    // Presence was cleared while the occupancy sensor still reads on: no
    // presence demand (source idle, no Clear button), but the hint tells the
    // user why instead of the page looking self-contradictory.
    vi.mocked(api.getRoomActiveStatuses).mockResolvedValue({
      "room-1": { ...idleStatus, presence_suppressed: true },
    });

    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    expect(
      await screen.findByText(/presence cleared — ignored until the room empties/i)
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Clear presence/i })).not.toBeInTheDocument();
  });

  it("does not show the suppression hint when presence is not suppressed", async () => {
    vi.mocked(api.getRoomActiveStatuses).mockResolvedValue({ "room-1": idleStatus });

    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    await screen.findByText("Living Room");
    expect(screen.queryByText(/ignored until the room empties/i)).not.toBeInTheDocument();
  });

  it("calls clearPresenceHoldover and refreshes statuses on click", async () => {
    vi.mocked(api.getRoomActiveStatuses).mockResolvedValue({ "room-1": activePresenceStatus });
    vi.mocked(api.clearPresenceHoldover).mockResolvedValue(undefined);

    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    const btn = await screen.findByRole("button", { name: /Clear presence/i });
    fireEvent.click(btn);

    await waitFor(() => {
      expect(api.clearPresenceHoldover).toHaveBeenCalledWith("room-1");
    });
    // Status refresh triggered — getRoomActiveStatuses called a second time
    await waitFor(() => {
      expect(api.getRoomActiveStatuses).toHaveBeenCalledTimes(2);
    });
  });

  it("shows a stale-sensor badge on the room card when the API reports one (Issue #211)", async () => {
    vi.mocked(api.getSensorHealth).mockResolvedValue({
      stale_after_min: 30,
      rooms: [
        {
          room_id: "room-1",
          room_name: "Living Room",
          thermostat_entity_id: "climate.test",
          stale_sensors: [{ entity_id: "sensor.dead_battery", age_seconds: 7200, reason: "stale" }],
        },
      ],
    });
    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );
    const badge = await screen.findByTestId("stale-badge-room-1");
    expect(badge).toHaveTextContent("1 stale sensor");
    // Title attribute carries the per-sensor detail used by browsers as a tooltip.
    expect(badge.getAttribute("title")).toContain("sensor.dead_battery");
  });

  // -------------------------------------------------------------------------
  // Ambient-aware presence suppression / pre-cool (Issue #248, Phase 4)
  // -------------------------------------------------------------------------

  const openNewRoom = async () => {
    fireEvent.click(await screen.findByText("+ Add room"));
    await screen.findByText("New Room", { selector: ".page-title" });
  };

  it("reveals pre-cool/pre-heat controls with worked examples when enabled", async () => {
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: "sensor.outdoor",
      current_value: 80,
    });
    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );
    await openNewRoom();

    const toggle = await screen.findByLabelText(/pre-cool \/ pre-heat/i);
    await waitFor(() => expect(toggle).not.toBeDisabled());
    fireEvent.click(toggle);

    // The minimum-differential field carries a concrete worked example.
    expect(screen.getByText(/only skips heating when it is at least/i)).toBeInTheDocument();
    // The widened-deadband control and mode select are revealed.
    expect(screen.getByLabelText(/Widened deadband/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/When to apply/i)).toBeInTheDocument();
  });

  it("disables pre-cool/pre-heat without an outside sensor", async () => {
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: null,
      current_value: null,
    });
    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );
    await openNewRoom();

    const toggle = await screen.findByLabelText(/pre-cool \/ pre-heat/i);
    await waitFor(() => expect(toggle).toBeDisabled());
    expect(screen.getByText(/Add an outside temperature sensor/i)).toBeInTheDocument();
  });

  it("blocks a widened deadband below the thermostat deadband", async () => {
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: "sensor.outdoor",
      current_value: 80,
    });
    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );
    await openNewRoom();

    fireEvent.change(screen.getByLabelText(/Room name/i), { target: { value: "X" } });
    fireEvent.change(screen.getByRole("combobox", { name: /Thermostat/i }), {
      target: { value: "climate.test" },
    });

    const toggle = await screen.findByLabelText(/pre-cool \/ pre-heat/i);
    await waitFor(() => expect(toggle).not.toBeDisabled());
    fireEvent.click(toggle);

    // Thermostat deadband is 0.5°F; 0.1 is below it -> blocked before any POST.
    fireEvent.change(screen.getByLabelText(/Widened deadband/i), { target: { value: "0.1" } });
    fireEvent.click(screen.getByRole("button", { name: /Create room/i }));

    expect(
      await screen.findByText(/widened deadband must be at least the thermostat's deadband/i)
    ).toBeInTheDocument();
    expect(api.createRoom).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Eco Mode per-room override (Issue #404)
// ---------------------------------------------------------------------------
describe("Rooms Page — Eco Mode override (#404)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getRooms).mockResolvedValue(mockRooms);
    vi.mocked(api.getThermostats).mockResolvedValue(mockThermostats);
    vi.mocked(api.getRoom).mockImplementation((id: string) =>
      Promise.resolve(mockRooms.find((r) => r.id === id) as api.Room)
    );
    vi.mocked(api.getRoomActiveStatuses).mockResolvedValue({
      "room-1": {
        room_id: "room-1",
        source: "idle",
        target_temp: null,
        ends_in_seconds: null,
        presence_holdover_active: false,
        presence_suppressed: false,
        next_schedule_in_seconds: null,
        next_schedule_target: null,
        next_schedule_label: null,
      },
    });
    vi.mocked(api.getEntityStates).mockResolvedValue({});
    vi.mocked(api.getHAEntities).mockResolvedValue([]);
    // Default: an outside sensor is configured so the "On" option is available.
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: "sensor.outdoor",
      current_value: 80,
    });
  });

  it("initializes tri-state enable and per-field overrides from an existing room, then saves them", async () => {
    // A room that forces Eco on and overrides two fields (one absolute, one
    // delta). Exercises the eco state-init branches and the payload builder.
    const ecoRoom: api.Room = {
      ...mockRooms[0],
      eco_mode_enabled: true,
      eco_cooling_outdoor_threshold: 90,
      eco_cooling_max_drift: 6,
    };
    vi.mocked(api.getRooms).mockResolvedValue([ecoRoom]);
    vi.mocked(api.getRoom).mockResolvedValue(ecoRoom);
    vi.mocked(api.updateRoom).mockResolvedValue(ecoRoom);

    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    fireEvent.click(await screen.findByRole("button", { name: /Settings/i }));

    // Tri-state enable initialised to "On for this room".
    const ecoSelect = screen.getByLabelText(/^Eco Mode$/) as HTMLSelectElement;
    expect(ecoSelect.value).toBe("on");

    // Wait for the outside-sensor probe to resolve so the save-time guard
    // (Eco on requires a sensor) does not block this legitimate save.
    const onOption = within(ecoSelect).getByRole("option", {
      name: /On for this room/i,
    }) as HTMLOptionElement;
    await waitFor(() => expect(onOption).not.toBeDisabled());

    // Per-field overrides initialised in display units (°F here).
    const threshInput = screen.getByLabelText(/Cooling.*outdoor threshold/i) as HTMLInputElement;
    expect(threshInput.value).toBe("90");
    const driftInput = screen.getByLabelText(/Cooling.*max drift/i) as HTMLInputElement;
    expect(driftInput.value).toBe("6");

    // Fields left blank inherit the thermostat — the placeholder echoes the
    // inherited value (heating threshold defaults to 40°F).
    const heatThresh = screen.getByLabelText(/Heating.*outdoor threshold/i) as HTMLInputElement;
    expect(heatThresh.value).toBe("");
    expect(heatThresh.placeholder).toMatch(/Inherit \(40°F\)/);

    // The worked example renders because a thermostat is selected.
    expect(screen.getByTestId("eco-worked-example")).toBeInTheDocument();

    // Edit an override field (covers the eco onChange handler).
    fireEvent.change(driftInput, { target: { value: "5" } });

    fireEvent.click(screen.getByRole("button", { name: /Save changes/i }));
    await waitFor(() => {
      expect(api.updateRoom).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({
          eco_mode_enabled: true,
          eco_cooling_outdoor_threshold: 90,
          eco_cooling_max_drift: 5,
          // Untouched fields stay null (inherit the thermostat).
          eco_heating_outdoor_threshold: null,
        })
      );
    });
  });

  it("initializes to Off for a room that opts out, and saves the tri-state as false", async () => {
    const offRoom: api.Room = { ...mockRooms[0], eco_mode_enabled: false };
    vi.mocked(api.getRooms).mockResolvedValue([offRoom]);
    vi.mocked(api.getRoom).mockResolvedValue(offRoom);
    vi.mocked(api.updateRoom).mockResolvedValue(offRoom);

    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    fireEvent.click(await screen.findByRole("button", { name: /Settings/i }));
    const ecoSelect = screen.getByLabelText(/^Eco Mode$/) as HTMLSelectElement;
    expect(ecoSelect.value).toBe("off");

    fireEvent.click(screen.getByRole("button", { name: /Save changes/i }));
    await waitFor(() => {
      expect(api.updateRoom).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({ eco_mode_enabled: false })
      );
    });
  });

  it("blocks forcing Eco on without an outside-temperature sensor and shows the hint", async () => {
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: null,
      current_value: null,
    });
    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    fireEvent.click(await screen.findByText("+ Add room"));
    await screen.findByText("New Room", { selector: ".page-title" });

    fireEvent.change(screen.getByLabelText(/Room name/i), { target: { value: "Den" } });
    fireEvent.change(screen.getByRole("combobox", { name: /Thermostat/i }), {
      target: { value: "climate.test" },
    });

    // The gating hint is shown when no sensor is configured.
    expect(screen.getByText(/PirateWeather/i)).toBeInTheDocument();

    // Force "on" (the option is disabled in the UI, but the save still guards).
    const ecoSelect = screen.getByLabelText(/^Eco Mode$/) as HTMLSelectElement;
    fireEvent.change(ecoSelect, { target: { value: "on" } });

    fireEvent.click(screen.getByRole("button", { name: /Create room/i }));

    expect(await screen.findByText(/before forcing Eco on for this room/i)).toBeInTheDocument();
    expect(api.createRoom).not.toHaveBeenCalled();
  });

  it("falls back to a neutral worked example when the thermostat carries no eco values", async () => {
    // A thermostat whose eco fields are all null: the inherited values resolve
    // to null so the placeholders read plain "Inherit" and the worked example
    // uses the 0 fallback rather than crashing.
    const bareThermo = {
      ...mockThermostats[0],
      thermostat_entity_id: "climate.bare",
      name: "Bare HVAC",
      eco_cooling_outdoor_threshold: null,
      eco_cooling_full_drift_temp: null,
      eco_cooling_max_drift: null,
      eco_heating_outdoor_threshold: null,
      eco_heating_full_drift_temp: null,
      eco_heating_max_drift: null,
      eco_hysteresis_band: null,
    } as unknown as api.ThermostatConfig;
    const bareRoom: api.Room = { ...mockRooms[0], thermostat_entity_id: "climate.bare" };
    vi.mocked(api.getThermostats).mockResolvedValue([bareThermo]);
    vi.mocked(api.getRooms).mockResolvedValue([bareRoom]);
    vi.mocked(api.getRoom).mockResolvedValue(bareRoom);

    render(
      <SystemContext.Provider value={mockSystem}>
        <Rooms />
      </SystemContext.Provider>
    );

    fireEvent.click(await screen.findByRole("button", { name: /Settings/i }));

    // Placeholder is a plain "Inherit" — no inherited value to echo.
    const threshInput = screen.getByLabelText(/Cooling.*outdoor threshold/i) as HTMLInputElement;
    expect(threshInput.placeholder).toBe("Inherit");

    // The worked example still renders (a thermostat is selected).
    expect(screen.getByTestId("eco-worked-example")).toBeInTheDocument();
  });
});
