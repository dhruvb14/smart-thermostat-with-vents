import { describe, it, expect, vi, beforeEach } from "vitest";
import { ecoRoomDefaults } from "../testFixtures";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
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
    ...ecoRoomDefaults,
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
    vi.mocked(api.deleteSchedule).mockResolvedValue({});

    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));

    const delBtn = await screen.findByText("Del");
    fireEvent.click(delBtn);

    const dialog = await screen.findByTestId("confirm-dialog");
    expect(within(dialog).getByText(/Delete this schedule/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(api.deleteSchedule).toHaveBeenCalledWith("room-1", "sched-1");
    });
  });

  it("does not delete a schedule when the confirmation dialog is cancelled", async () => {
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));

    const delBtn = await screen.findByText("Del");
    fireEvent.click(delBtn);

    const dialog = await screen.findByTestId("confirm-dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(screen.queryByTestId("confirm-dialog")).not.toBeInTheDocument();
    expect(api.deleteSchedule).not.toHaveBeenCalled();
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

  it("client overlap check ignores a disabled existing block", async () => {
    // A parked (disabled) block must not reserve its slot client-side — adding
    // an overlapping block should save, not show the overlap error (#359).
    vi.mocked(api.getSchedules).mockResolvedValue([{ ...mockSchedules[0], enabled: false }]);
    vi.mocked(api.createSchedule).mockResolvedValue({ ...mockSchedules[0], id: "new" });
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("+ Add schedule block"));
    // Modal defaults to 22:00–07:00 Mon–Fri, same as the parked block.
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => {
      expect(api.createSchedule).toHaveBeenCalled();
    });
    expect(screen.queryByText(/Overlaps with existing block/)).not.toBeInTheDocument();
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

// ---------------------------------------------------------------------------
// Per-schedule deadband override (Issue #517)
//
// The band is a DELTA, so init must go through toDisplayDelta (× 5/9) and NOT
// toDisplay (which would also subtract 32). The payload carries the RAW display
// value — the backend's _delta_to_f converts it (#231).
// ---------------------------------------------------------------------------

const bandedSchedule: api.Schedule = {
  ...mockSchedules[0],
  id: "sched-band",
  // 1.8 °F delta → toDisplayDelta → 1 °C. (toDisplay would give -16.8 °C.)
  deadband_override: 1.8,
};

const createdSchedule: api.Schedule = {
  id: "sched-2",
  room_id: "room-1",
  days_of_week: [],
  start_time: "",
  end_time: "",
  target_temp: 72,
  enabled: true,
  expires_at: null,
};

/** Open the "New schedule" modal with valid times already filled in. */
const openNewBlock = async () => {
  fireEvent.click(await screen.findByText("Living Room"));
  fireEvent.click(await screen.findByText("+ Add schedule block"));
  fireEvent.change(screen.getByLabelText(/Start time/i), { target: { value: "10:00" } });
  fireEvent.change(screen.getByLabelText(/End time/i), { target: { value: "12:00" } });
};

const inheritRadio = () => screen.getByRole("radio", { name: /normal deadband/i });
const customRadio = () => screen.getByRole("radio", { name: /extra drift/i });
const bandInput = () => screen.getByLabelText(/^Deadband/i);

describe("Schedules Page — per-schedule deadband override (#517)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getRooms).mockResolvedValue(mockRooms);
    vi.mocked(api.getSchedules).mockResolvedValue(mockSchedules);
    vi.mocked(api.createSchedule).mockResolvedValue(createdSchedule);
    vi.mocked(api.updateSchedule).mockResolvedValue(createdSchedule);
  });

  it("defaults to inherit and hides the number input for a block with no band", async () => {
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("Edit"));

    expect(inheritRadio()).toBeChecked();
    expect(customRadio()).not.toBeChecked();
    expect(screen.queryByLabelText(/^Deadband/i)).not.toBeInTheDocument();
  });

  it("opens in custom mode with the input pre-filled for a block that has a band", async () => {
    vi.mocked(api.getSchedules).mockResolvedValue([bandedSchedule]);
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("Edit"));

    expect(customRadio()).toBeChecked();
    expect(inheritRadio()).not.toBeChecked();
    expect(bandInput()).toHaveValue(1.8);
  });

  it("shows the input when custom is picked and hides it again on inherit", async () => {
    render(<Schedules />);
    await openNewBlock();

    // Off by default.
    expect(screen.queryByLabelText(/^Deadband/i)).not.toBeInTheDocument();

    fireEvent.click(customRadio());
    expect(bandInput()).toBeInTheDocument();

    fireEvent.click(inheritRadio());
    expect(screen.queryByLabelText(/^Deadband/i)).not.toBeInTheDocument();
  });

  it("sends deadband_override:null when left on the room's normal deadband", async () => {
    render(<Schedules />);
    await openNewBlock();
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => {
      expect(api.createSchedule).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({ deadband_override: null })
      );
    });
  });

  it("sends the typed band when custom is picked", async () => {
    render(<Schedules />);
    await openNewBlock();
    fireEvent.click(customRadio());
    fireEvent.change(bandInput(), { target: { value: "4" } });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => {
      expect(api.createSchedule).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({ deadband_override: 4 })
      );
    });
  });

  it("clears an existing band back to null when switched to inherit", async () => {
    vi.mocked(api.getSchedules).mockResolvedValue([bandedSchedule]);
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("Edit"));

    fireEvent.click(inheritRadio());
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => {
      expect(api.updateSchedule).toHaveBeenCalledWith(
        "room-1",
        "sched-band",
        expect.objectContaining({ deadband_override: null })
      );
    });
  });

  it.each([
    ["empty", ""],
    ["non-numeric", "abc"],
    ["negative", "-1"],
    ["above the max", "10.5"],
  ])("rejects a %s band and does not submit", async (_label, value) => {
    render(<Schedules />);
    await openNewBlock();
    fireEvent.click(customRadio());
    fireEvent.change(bandInput(), { target: { value } });
    fireEvent.click(screen.getByText("Save"));

    expect(await screen.findByText(/Deadband must be between/i)).toBeInTheDocument();
    expect(api.createSchedule).not.toHaveBeenCalled();
  });

  it.each([
    ["the lower bound", "0"],
    ["the upper bound", "10"],
  ])("accepts %s", async (_label, value) => {
    render(<Schedules />);
    await openNewBlock();
    fireEvent.click(customRadio());
    fireEvent.change(bandInput(), { target: { value } });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => {
      expect(api.createSchedule).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({ deadband_override: parseFloat(value) })
      );
    });
  });

  it("badges a row that carries a band, and leaves an unbanded row bare", async () => {
    vi.mocked(api.getSchedules).mockResolvedValue([bandedSchedule]);
    const { unmount } = render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));
    expect(await screen.findByText(/±1\.8°F drift/)).toBeInTheDocument();
    unmount();

    vi.mocked(api.getSchedules).mockResolvedValue(mockSchedules);
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));
    await screen.findByText("Edit");
    expect(screen.queryByText(/drift/i)).not.toBeInTheDocument();
  });
});

describe("Schedules Page — deadband override in Celsius mode (#517)", () => {
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
    vi.mocked(api.createSchedule).mockResolvedValue(createdSchedule);
  });

  it("labels the input in °C", async () => {
    renderInCelsius();
    await openNewBlock();
    fireEvent.click(customRadio());
    expect(screen.getByLabelText("Deadband (°C)")).toBeInTheDocument();
  });

  it("initialises a stored band via toDisplayDelta, not toDisplay", async () => {
    // 1.8 °F is a DELTA: × 5/9 = 1 °C. Going through toDisplay would subtract
    // 32 first and render -16.8 — the bug this guards against.
    vi.mocked(api.getSchedules).mockResolvedValue([bandedSchedule]);
    renderInCelsius();
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("Edit"));

    expect(bandInput()).toHaveValue(1);
  });

  it("sends the user's raw °C band, not a pre-converted °F value (#231)", async () => {
    renderInCelsius();
    await openNewBlock();
    fireEvent.click(customRadio());
    fireEvent.change(bandInput(), { target: { value: "2" } });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => {
      expect(api.createSchedule).toHaveBeenCalledWith(
        "room-1",
        // 2, NOT 3.6 (toStorageDelta) and NOT 35.6 (toStorage).
        expect.objectContaining({ deadband_override: 2 })
      );
    });
  });

  it("bounds the band in °C (10°F max → 5.56°C)", async () => {
    renderInCelsius();
    await openNewBlock();
    fireEvent.click(customRadio());
    fireEvent.change(bandInput(), { target: { value: "6" } });
    fireEvent.click(screen.getByText("Save"));

    expect(await screen.findByText(/5\.56°C/)).toBeInTheDocument();
    expect(api.createSchedule).not.toHaveBeenCalled();
  });

  it("renders the row badge as a °C delta", async () => {
    vi.mocked(api.getSchedules).mockResolvedValue([bandedSchedule]);
    renderInCelsius();
    fireEvent.click(await screen.findByText("Living Room"));
    expect(await screen.findByText(/±1°C drift/)).toBeInTheDocument();
  });
});
