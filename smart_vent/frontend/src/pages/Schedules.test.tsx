import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import Schedules from "./Schedules";
import * as api from "../api";
import { UnitContext, buildUnitContext } from "../contexts";

vi.mock("../api");

const mockRooms: api.Room[] = [
  {
    id: "room-1",
    name: "Living Room",
    thermostat_entity_id: "climate.test",
    sensors: [],
    vents: [],
    presence_sensors: [],
    include_thermostat_sensor: false,
    presence_holdover_hours: 2,
    temp_offset: 0,
    deadband_override: null,
    notes: "",
    system_wide_temp: null,
    ambient_suppression_enabled: false,
    ambient_suppression_mode: "any_presence",
    ambient_suppression_min_differential: 5,
    ambient_suppression_deadband: 2,
    ambient_suppression_off_schedule_window_min: 60,
  },
];

const mockSchedules: api.Schedule[] = [
  {
    id: "sched-1",
    room_id: "room-1",
    days_of_week: [0, 1, 2, 3, 4],
    start_time: "22:00",
    end_time: "07:00",
    target_temp: 68,
    enabled: true,
    expires_at: null,
  },
];

describe("Schedules Page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getRooms).mockResolvedValue(mockRooms);
    vi.mocked(api.getSchedules).mockResolvedValue(mockSchedules);
  });

  it("renders the rooms with schedule counts", async () => {
    render(<Schedules />);
    expect(await screen.findByText("Living Room")).toBeInTheDocument();
  });

  it("shows validation error for overlapping schedule", async () => {
    render(<Schedules />);

    // Expand the room
    fireEvent.click(await screen.findByText("Living Room"));

    // Click Add
    fireEvent.click(await screen.findByText("+ Add schedule block"));

    // Set overlapping time (default in modal is 22:00-07:00, same as mock)
    // Click Save
    fireEvent.click(screen.getByText("Save"));

    expect(await screen.findByText(/Overlaps with existing block/)).toBeInTheDocument();
  });

  it("successfully adds a schedule", async () => {
    vi.mocked(api.createSchedule).mockResolvedValue({
      id: "sched-2",
      room_id: "room-1",
      days_of_week: [],
      start_time: "",
      end_time: "",
      target_temp: 72,
      enabled: true,
      expires_at: null,
    });

    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("+ Add schedule block"));

    const startInput = screen.getByLabelText(/Start time/i);
    const endInput = screen.getByLabelText(/End time/i);

    fireEvent.change(startInput, { target: { value: "10:00" } });
    fireEvent.change(endInput, { target: { value: "12:00" } });

    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => {
      expect(api.createSchedule).toHaveBeenCalled();
    });
  });

  it("successfully updates a schedule", async () => {
    vi.mocked(api.updateSchedule).mockResolvedValue({
      id: "sched-1",
      room_id: "room-1",
      days_of_week: [],
      start_time: "",
      end_time: "",
      target_temp: 70,
      enabled: true,
      expires_at: null,
    });

    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));

    const editBtn = await screen.findByText("Edit");
    fireEvent.click(editBtn);

    const tempInput = screen.getByLabelText(/Target temperature/i);
    fireEvent.change(tempInput, { target: { value: "70" } });

    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => {
      expect(api.updateSchedule).toHaveBeenCalled();
    });
  });

  it("successfully deletes a schedule", async () => {
    window.confirm = vi.fn().mockReturnValue(true);
    vi.mocked(api.deleteSchedule).mockResolvedValue({});

    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));

    const delBtn = await screen.findByText("Del");
    fireEvent.click(delBtn);

    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => {
      expect(api.deleteSchedule).toHaveBeenCalledWith("room-1", "sched-1");
    });
  });
});

describe("Schedules Page — Celsius mode", () => {
  const renderInCelsius = () =>
    render(
      <UnitContext.Provider value={buildUnitContext("C")}>
        <Schedules />
      </UnitContext.Provider>
    );

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getRooms).mockResolvedValue(mockRooms);
    vi.mocked(api.getSchedules).mockResolvedValue(mockSchedules);
  });

  it("shows temperature label in °C", async () => {
    renderInCelsius();
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("+ Add schedule block"));
    expect(screen.getByText("Target temperature (°C)")).toBeInTheDocument();
  });

  it("displays existing schedule target temp in °C", async () => {
    // mockSchedules[0].target_temp = 68°F → fmtTemp(68) = "20.0°C"
    renderInCelsius();
    fireEvent.click(await screen.findByText("Living Room"));
    expect(await screen.findByText("20.0°C")).toBeInTheDocument();
  });

  it("shows validation error bounds in °C", async () => {
    renderInCelsius();
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("+ Add schedule block"));

    const tempInput = screen.getByLabelText(/Target temperature/i);
    fireEvent.change(tempInput, { target: { value: "0" } }); // 0°C < 4.4°C lower bound
    fireEvent.click(screen.getByText("Save"));

    expect(await screen.findByText(/4\.4°C and 32\.2°C/)).toBeInTheDocument();
  });

  it("sends the user's raw °C target_temp when saving a schedule (#231)", async () => {
    vi.mocked(api.createSchedule).mockResolvedValue({
      id: "sched-2",
      room_id: "room-1",
      days_of_week: [],
      start_time: "",
      end_time: "",
      target_temp: 71.6,
      enabled: true,
      expires_at: null,
    });

    renderInCelsius();
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("+ Add schedule block"));

    fireEvent.change(screen.getByLabelText(/Start time/i), { target: { value: "10:00" } });
    fireEvent.change(screen.getByLabelText(/End time/i), { target: { value: "12:00" } });
    // Frontend sends display value as-is; backend's _to_f converts °C → °F.
    fireEvent.change(screen.getByLabelText(/Target temperature/i), { target: { value: "22" } });

    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => {
      expect(api.createSchedule).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({ target_temp: 22 })
      );
    });
  });
});

describe("Schedules Page — lifecycle (#359)", () => {
  const secondRoom: api.Room = { ...mockRooms[0], id: "room-2", name: "Bedroom" };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getRooms).mockResolvedValue(mockRooms);
    vi.mocked(api.getSchedules).mockResolvedValue(mockSchedules);
  });

  it("shows active and inactive badge counts", async () => {
    vi.mocked(api.getSchedules).mockResolvedValue([
      { ...mockSchedules[0], id: "a", enabled: true },
      { ...mockSchedules[0], id: "b", enabled: false, start_time: "08:00", end_time: "09:00" },
    ]);
    render(<Schedules />);
    expect(await screen.findByText("1 active")).toBeInTheDocument();
    expect(await screen.findByText("1 inactive")).toBeInTheDocument();
  });

  it("disabling a block sends enabled:false", async () => {
    vi.mocked(api.updateSchedule).mockResolvedValue({ ...mockSchedules[0], enabled: false });
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("Disable"));
    await waitFor(() => {
      expect(api.updateSchedule).toHaveBeenCalledWith("room-1", "sched-1", { enabled: false });
    });
  });

  it("surfaces a backend error when enabling conflicts", async () => {
    vi.mocked(api.getSchedules).mockResolvedValue([{ ...mockSchedules[0], enabled: false }]);
    vi.mocked(api.updateSchedule).mockRejectedValue(
      new Error("Overlaps with existing block on Mon 22:00–07:00")
    );
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("Enable"));
    expect(await screen.findByText(/Overlaps with existing block/)).toBeInTheDocument();
  });

  it("sends expires_at when Auto-disable at is chosen", async () => {
    vi.mocked(api.createSchedule).mockResolvedValue({
      ...mockSchedules[0],
      id: "new",
      expires_at: "2030-01-01T08:00",
    });
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("+ Add schedule block"));
    fireEvent.change(screen.getByLabelText(/Start time/i), { target: { value: "10:00" } });
    fireEvent.change(screen.getByLabelText(/End time/i), { target: { value: "12:00" } });
    fireEvent.click(screen.getByLabelText("Auto-disable at"));
    fireEvent.change(screen.getByLabelText("Auto-disable date and time"), {
      target: { value: "2030-01-01T08:00" },
    });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => {
      expect(api.createSchedule).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({ expires_at: "2030-01-01T08:00" })
      );
    });
  });

  it("sends expires_at:null when Never expire is chosen", async () => {
    vi.mocked(api.createSchedule).mockResolvedValue({ ...mockSchedules[0], id: "new" });
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("+ Add schedule block"));
    fireEvent.change(screen.getByLabelText(/Start time/i), { target: { value: "10:00" } });
    fireEvent.change(screen.getByLabelText(/End time/i), { target: { value: "12:00" } });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => {
      expect(api.createSchedule).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({ expires_at: null })
      );
    });
  });

  it("copies a schedule to selected rooms and shows results", async () => {
    vi.mocked(api.getRooms).mockResolvedValue([mockRooms[0], secondRoom]);
    vi.mocked(api.copySchedule).mockResolvedValue([
      { room_id: "room-2", schedule_id: "copy-1", status: "created", conflict_with: null },
    ]);
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("Copy"));
    fireEvent.click(await screen.findByLabelText("Bedroom"));
    // The modal's Copy button is the second "Copy" in the DOM (row + modal).
    const copyButtons = screen.getAllByText("Copy");
    fireEvent.click(copyButtons[copyButtons.length - 1]);
    await waitFor(() => {
      expect(api.copySchedule).toHaveBeenCalledWith("room-1", "sched-1", ["room-2"]);
    });
    expect(await screen.findByText("Copied")).toBeInTheDocument();
  });
});
