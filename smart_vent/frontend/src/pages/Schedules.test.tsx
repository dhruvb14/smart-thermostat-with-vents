import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import Schedules from "./Schedules";
import * as api from "../api";

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
    notes: "",
    system_wide_temp: null,
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
  },
];

describe("Schedules Page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.getRooms as any).mockResolvedValue(mockRooms);
    (api.getSchedules as any).mockResolvedValue(mockSchedules);
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
    (api.createSchedule as any).mockResolvedValue({ id: "sched-2" });

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
    (api.updateSchedule as any).mockResolvedValue({ id: "sched-1" });

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
    (api.deleteSchedule as any).mockResolvedValue({});

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
