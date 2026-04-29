import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import Rooms from "./Rooms";
import * as api from "../api";
import { SystemContext } from "../contexts";

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
