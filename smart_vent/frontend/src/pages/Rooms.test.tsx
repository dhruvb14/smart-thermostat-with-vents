import { describe, it, expect, vi, beforeEach } from "vitest"; // trigger CI
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
    min_open_vents: 1,
    overshoot_delta: 0.5,
    cycle_timeout_hours: 2,
    reconciliation_interval_min: 5,
    vacation_hvac_mode: "single" as const,
    min_cycle_runtime_min: 0,
    min_cycle_offtime_min: 0,
    cooling_lockout_below_f: null,
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
    system_wide_temp: 72,
    notes: "",
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

    await screen.findByText("New Room", { selector: ".modal-title" });

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

    await screen.findByText("New Room", { selector: ".modal-title" });

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

  it("shows presence temp and offset labels in °C in edit modal", async () => {
    renderInCelsius();
    const editBtn = await screen.findByRole("button", { name: /Settings/i });
    fireEvent.click(editBtn);

    expect(screen.getByText(/Presence-triggered temperature \(°C\)/i)).toBeInTheDocument();
    expect(screen.getByText(/Temperature offset \(°C\)/i)).toBeInTheDocument();
  });

  it("pre-populates system_wide_temp input in °C in edit modal", async () => {
    // mockRooms[0].system_wide_temp = 72°F → toDisplay(72) = 22.2°C
    renderInCelsius();
    const editBtn = await screen.findByRole("button", { name: /Settings/i });
    fireEvent.click(editBtn);

    const sysTempInput = screen.getByLabelText(
      /Presence-triggered temperature/i
    ) as HTMLInputElement;
    expect(parseFloat(sysTempInput.value)).toBeCloseTo(22.2, 1);
  });

  it("converts °C system_wide_temp input to °F when updating room", async () => {
    vi.mocked(api.updateRoom).mockResolvedValue({ ...mockRooms[0], system_wide_temp: 71.6 });

    renderInCelsius();
    const editBtn = await screen.findByRole("button", { name: /Settings/i });
    fireEvent.click(editBtn);

    const sysTempInput = screen.getByLabelText(/Presence-triggered temperature/i);
    // 22°C → toStorage(22) = 22 * 9/5 + 32 = 71.6°F
    fireEvent.change(sysTempInput, { target: { value: "22" } });

    fireEvent.click(screen.getByRole("button", { name: /Save changes/i }));

    await waitFor(() => {
      expect(api.updateRoom).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({ system_wide_temp: 71.6 })
      );
    });
  });
});

describe("Rooms Page — Clear presence button", () => {
  const activePresenceStatus: api.RoomActiveStatus = {
    room_id: "room-1",
    source: "presence",
    target_temp: 72,
    ends_in_seconds: 3600,
    presence_holdover_active: true,
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
          stale_sensors: [
            { entity_id: "sensor.dead_battery", age_seconds: 7200, reason: "stale" },
          ],
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
});
