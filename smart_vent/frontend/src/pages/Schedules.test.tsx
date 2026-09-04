import { describe, it, expect, vi, beforeEach } from "vitest";
import { ecoRoomDefaults, makeHold } from "../testFixtures";
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
    vi.mocked(api.getOverrides).mockResolvedValue([]);
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

    // Assert the whole payload, not just that the call happened: a bare
    // toHaveBeenCalled() passed with any of the times, days or target wired
    // wrong, so it pinned nothing about what the form actually submits.
    await waitFor(() => {
      expect(api.createSchedule).toHaveBeenCalledWith("room-1", {
        days_of_week: [0, 1, 2, 3, 4],
        start_time: "10:00",
        end_time: "12:00",
        target_temp: 72,
        enabled: true,
        deadband_override: null,
        expires_at: null,
        name: null,
      });
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

    // The edited target must reach the PUT, addressed at the block being
    // edited — a bare toHaveBeenCalled() passed even if the typed 70 never
    // made it into the payload.
    await waitFor(() => {
      expect(api.updateSchedule).toHaveBeenCalledWith(
        "room-1",
        "sched-1",
        expect.objectContaining({
          target_temp: 70,
          days_of_week: [0, 1, 2, 3, 4],
          start_time: "22:00",
          end_time: "07:00",
          enabled: true,
        })
      );
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
    vi.mocked(api.getOverrides).mockResolvedValue([]);
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

    // 4.5, not 4.4: toDisplay(40) rounds to 4.4 °C, which converts back to
    // 39.92 °F and the backend refuses it. The form must advertise a minimum
    // that can actually be saved (#521).
    expect(await screen.findByText(/4\.5°C and 32\.2°C/)).toBeInTheDocument();
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
    vi.mocked(api.getOverrides).mockResolvedValue([]);
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
const customRadio = () => screen.getByRole("radio", { name: /override deadband/i });
const bandInput = () => screen.getByLabelText(/^Deadband/i);

describe("Schedules Page — per-schedule deadband override (#517)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getRooms).mockResolvedValue(mockRooms);
    vi.mocked(api.getSchedules).mockResolvedValue(mockSchedules);
    vi.mocked(api.createSchedule).mockResolvedValue(createdSchedule);
    vi.mocked(api.updateSchedule).mockResolvedValue(createdSchedule);
    vi.mocked(api.getOverrides).mockResolvedValue([]);
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

  it("says the band REPLACES the room's, and names the value it replaces", async () => {
    // The control read as additive ("allow extra drift"), so a user setting 1
    // on a room whose band is 3 could reasonably expect 4. It replaces: the
    // effective band becomes 1, i.e. TIGHTER. The copy has to say so, and show
    // the number being replaced, or the control is a trap.
    vi.mocked(api.getRooms).mockResolvedValue([{ ...mockRooms[0], deadband_override: 3 }]);
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("+ Add schedule block"));

    // Inherit mode names the value that stays in force.
    expect(screen.getByText(/keeps its usual deadband/i)).toBeInTheDocument();
    expect(screen.getByText(/±3°F/)).toBeInTheDocument();

    fireEvent.click(customRadio());
    fireEvent.change(bandInput(), { target: { value: "1" } });

    // Custom mode states replacement explicitly and contrasts the two numbers.
    expect(screen.getByText(/replaces/i)).toBeInTheDocument();
    expect(screen.getByText(/is not added to it/i)).toBeInTheDocument();
    expect(screen.getByText(/instead of its usual/i)).toBeInTheDocument();
  });

  it("treats a zero band as set, not as absent", async () => {
    // 0 is a real band meaning "hold exactly, no tolerance". It is falsy, so a
    // truthiness check here would silently fall back to inherit and drop the
    // user's setting — the same trap `is not None` guards on the backend.
    vi.mocked(api.getSchedules).mockResolvedValue([
      { ...mockSchedules[0], id: "sched-zero", deadband_override: 0 },
    ]);
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));

    expect(await screen.findByText(/±0°F drift/)).toBeInTheDocument();

    fireEvent.click(screen.getByText("Edit"));
    expect(customRadio()).toBeChecked();
    expect(bandInput()).toHaveValue(0);
  });

  it("round-trips a zero band back out unchanged", async () => {
    vi.mocked(api.getSchedules).mockResolvedValue([
      { ...mockSchedules[0], id: "sched-zero", deadband_override: 0 },
    ]);
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("Edit"));
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => {
      expect(api.updateSchedule).toHaveBeenCalledWith(
        "room-1",
        "sched-zero",
        expect.objectContaining({ deadband_override: 0 })
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
    vi.mocked(api.getOverrides).mockResolvedValue([]);
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

  it("bounds the band in °C", async () => {
    renderInCelsius();
    await openNewBlock();
    fireEvent.click(customRadio());
    fireEvent.change(bandInput(), { target: { value: "6" } });
    fireEvent.click(screen.getByText("Save"));

    expect(await screen.findByText(/Deadband must be between/i)).toBeInTheDocument();
    expect(api.createSchedule).not.toHaveBeenCalled();
  });

  it("keeps a stored 10°F band editable in °C instead of wedging the block", async () => {
    // 10 °F is the documented maximum and the backend accepts it inclusively,
    // but toDisplayDelta(10) is 5.56, which converts back to 10.01 and is
    // refused. Unclamped, a °C household opening such a block gets 5.56 in a
    // field capped at 5.55 and every save is rejected — including edits to
    // fields the user actually touched.
    vi.mocked(api.getSchedules).mockResolvedValue([
      { ...mockSchedules[0], id: "sched-max", deadband_override: 10 },
    ]);
    renderInCelsius();
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("Edit"));

    expect(bandInput()).toHaveValue(5.55);

    // An edit to an unrelated field still saves.
    fireEvent.change(screen.getByLabelText(/Target temperature/i), { target: { value: "19" } });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => {
      expect(api.updateSchedule).toHaveBeenCalledWith(
        "room-1",
        "sched-max",
        expect.objectContaining({ target_temp: 19, deadband_override: 5.55 })
      );
    });
  });

  it("advertises a °C maximum the backend will actually accept", async () => {
    // toDisplayDelta(10) rounds 5.5555… UP to 5.56, which converts back to
    // 10.01 °F and fails the backend's 0–10 check. Advertising 5.56 would mean
    // the form's own stated maximum 400s on save. The cap is stepped down to
    // 5.55 (→ 9.99 °F) instead.
    renderInCelsius();
    await openNewBlock();
    fireEvent.click(customRadio());
    expect(bandInput()).toHaveAttribute("max", "5.55");
  });

  it("rejects 5.56°C, which would convert to 10.01°F and be refused (#517)", async () => {
    renderInCelsius();
    await openNewBlock();
    fireEvent.click(customRadio());
    fireEvent.change(bandInput(), { target: { value: "5.56" } });
    fireEvent.click(screen.getByText("Save"));

    expect(await screen.findByText(/Deadband must be between/i)).toBeInTheDocument();
    expect(api.createSchedule).not.toHaveBeenCalled();
  });

  it("accepts 5.55°C, the largest band that survives the round trip", async () => {
    renderInCelsius();
    await openNewBlock();
    fireEvent.click(customRadio());
    fireEvent.change(bandInput(), { target: { value: "5.55" } });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => {
      expect(api.createSchedule).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({ deadband_override: 5.55 })
      );
    });
  });

  it("renders the row badge as a °C delta", async () => {
    vi.mocked(api.getSchedules).mockResolvedValue([bandedSchedule]);
    renderInCelsius();
    fireEvent.click(await screen.findByText("Living Room"));
    expect(await screen.findByText(/±1°C drift/)).toBeInTheDocument();
  });
});

// ── Optional display name + always-visible ID (#520) ────────────────────────

const namedSchedule: api.Schedule = {
  ...mockSchedules[0],
  id: "sched-named",
  name: "Weekday night setback",
};

const nameInput = () => screen.getByLabelText(/^Name/i);

describe("Schedules Page — schedule display name (#520)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getRooms).mockResolvedValue(mockRooms);
    vi.mocked(api.getSchedules).mockResolvedValue(mockSchedules);
    vi.mocked(api.createSchedule).mockResolvedValue(createdSchedule);
    vi.mocked(api.updateSchedule).mockResolvedValue(createdSchedule);
    vi.mocked(api.getOverrides).mockResolvedValue([]);
  });

  it("offers an empty name field on a new block", async () => {
    render(<Schedules />);
    await openNewBlock();
    expect(nameInput()).toHaveValue("");
  });

  it("pre-fills the name when editing a named block", async () => {
    vi.mocked(api.getSchedules).mockResolvedValue([namedSchedule]);
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("Edit"));

    expect(nameInput()).toHaveValue("Weekday night setback");
  });

  it("sends the typed name, trimmed", async () => {
    render(<Schedules />);
    await openNewBlock();
    fireEvent.change(nameInput(), { target: { value: "  Guest stay  " } });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => {
      expect(api.createSchedule).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({ name: "Guest stay" })
      );
    });
  });

  it("sends name:null when the field is left blank", async () => {
    render(<Schedules />);
    await openNewBlock();
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => {
      expect(api.createSchedule).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({ name: null })
      );
    });
  });

  it("sends name:null when a whitespace-only name is typed", async () => {
    render(<Schedules />);
    await openNewBlock();
    fireEvent.change(nameInput(), { target: { value: "   " } });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => {
      expect(api.createSchedule).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({ name: null })
      );
    });
  });

  it("clears an existing name back to null when the field is emptied", async () => {
    vi.mocked(api.getSchedules).mockResolvedValue([namedSchedule]);
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("Edit"));
    fireEvent.change(nameInput(), { target: { value: "" } });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => {
      expect(api.updateSchedule).toHaveBeenCalledWith(
        "room-1",
        "sched-named",
        expect.objectContaining({ name: null })
      );
    });
  });

  it("caps the field at the length the backend accepts", async () => {
    render(<Schedules />);
    await openNewBlock();
    expect(nameInput()).toHaveAttribute("maxLength", "64");
  });

  it("shows the name in the row", async () => {
    vi.mocked(api.getSchedules).mockResolvedValue([namedSchedule]);
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));
    expect(await screen.findByText("Weekday night setback")).toBeInTheDocument();
  });

  it("marks an un-named block as Unnamed rather than leaving the cell blank", async () => {
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));
    expect(await screen.findByText("Unnamed")).toBeInTheDocument();
  });

  it("exposes the block's id as a hover tooltip on a named block", async () => {
    // The id is what addresses this block from the REST/MCP APIs (and, per
    // #519, from an MQTT command topic), so naming a block must not hide it.
    vi.mocked(api.getSchedules).mockResolvedValue([namedSchedule]);
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));

    const chip = await screen.findByTestId("schedule-id");
    expect(chip).toHaveAttribute("title", "Schedule ID: sched-named");
  });

  it("exposes the block's id as a hover tooltip on an un-named block too", async () => {
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));

    const chip = await screen.findByTestId("schedule-id");
    expect(chip).toHaveAttribute("title", "Schedule ID: sched-1");
  });

  it("reveals the full id as selectable text when the chip is tapped", async () => {
    // A `title` tooltip never opens on a touch screen, so hover alone would put
    // the id out of reach on a phone — where this app is most often used.
    vi.mocked(api.getSchedules).mockResolvedValue([namedSchedule]);
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));

    const chip = await screen.findByTestId("schedule-id");
    expect(chip).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("sched-named")).not.toBeInTheDocument();

    fireEvent.click(chip);
    expect(await screen.findByText("sched-named")).toBeInTheDocument();
    expect(chip).toHaveAttribute("aria-expanded", "true");

    // Tapping again hides it, so the row returns to its compact form.
    fireEvent.click(chip);
    expect(screen.queryByText("sched-named")).not.toBeInTheDocument();
  });

  it("reveals the id from the keyboard too", async () => {
    vi.mocked(api.getSchedules).mockResolvedValue([namedSchedule]);
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));

    const chip = await screen.findByTestId("schedule-id");
    fireEvent.keyDown(chip, { key: "Enter" });
    expect(await screen.findByText("sched-named")).toBeInTheDocument();
  });

  it("ignores other keys on the chip", async () => {
    vi.mocked(api.getSchedules).mockResolvedValue([namedSchedule]);
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));

    const chip = await screen.findByTestId("schedule-id");
    fireEvent.keyDown(chip, { key: "a" });
    expect(screen.queryByText("sched-named")).not.toBeInTheDocument();
  });

  it("keeps the id out of the rendered text until asked, so screenshots stay stable", async () => {
    // A GUID is regenerated per install; rendering it would churn every golden.
    vi.mocked(api.getSchedules).mockResolvedValue([namedSchedule]);
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));

    const chip = await screen.findByTestId("schedule-id");
    expect(chip).toHaveTextContent("ID");
    expect(chip.textContent).not.toContain("sched-named");
  });
});

// ── Temporary hold card above the schedule table (#576) ─────────────────────

describe("Schedules Page — temporary holds (#576)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getRooms).mockResolvedValue(mockRooms);
    vi.mocked(api.getSchedules).mockResolvedValue(mockSchedules);
    vi.mocked(api.getOverrides).mockResolvedValue([makeHold()]);
  });

  it("shows the hold card above the blocks when the room carries a hold", async () => {
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));

    const card = await screen.findByTestId("hold-card-room-1");
    expect(card).toHaveTextContent("Temporary hold");
    // Target goes through fmtTemp; the countdown and Eco tag sit beside it.
    expect(card).toHaveTextContent("75.0°F");
    expect(card).toHaveTextContent("ends in 1h 30m");
    expect(card).toHaveTextContent("ignores Eco");
    expect(within(card).getByRole("button", { name: "Manage hold" })).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Cancel hold" })).toBeInTheDocument();
    // While a hold is live the footer entry point gives way to the card.
    expect(screen.queryByText(/Set temporary hold/)).not.toBeInTheDocument();
  });

  it("labels an Eco-opted-in hold as relaxable", async () => {
    vi.mocked(api.getOverrides).mockResolvedValue([makeHold({ respect_eco: true })]);
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));

    const card = await screen.findByTestId("hold-card-room-1");
    expect(card).toHaveTextContent("Eco may relax");
    expect(card).not.toHaveTextContent("ignores Eco");
  });

  it("cancels the hold from the card and refetches the holds", async () => {
    // First fetch carries the hold; the post-cancel refetch comes back empty.
    vi.mocked(api.getOverrides).mockResolvedValueOnce([makeHold()]).mockResolvedValue([]);
    vi.mocked(api.clearOverride).mockResolvedValue({});
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));

    const card = await screen.findByTestId("hold-card-room-1");
    fireEvent.click(within(card).getByRole("button", { name: "Cancel hold" }));

    await waitFor(() => {
      expect(api.clearOverride).toHaveBeenCalledWith("room-1");
    });
    // The refetch clears the card and the footer entry point returns.
    await waitFor(() => {
      expect(screen.queryByTestId("hold-card-room-1")).not.toBeInTheDocument();
    });
    expect(api.getOverrides).toHaveBeenCalledTimes(2);
    expect(screen.getByText(/Set temporary hold/)).toBeInTheDocument();
  });

  it("surfaces a cancel failure without dropping the card", async () => {
    vi.mocked(api.clearOverride).mockRejectedValue(new Error("Room not found"));
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));

    const card = await screen.findByTestId("hold-card-room-1");
    fireEvent.click(within(card).getByRole("button", { name: "Cancel hold" }));

    expect(await screen.findByText("Room not found")).toBeInTheDocument();
    expect(screen.getByTestId("hold-card-room-1")).toBeInTheDocument();
  });

  it("offers Set temporary hold in the footer when no hold exists and opens the modal", async () => {
    vi.mocked(api.getOverrides).mockResolvedValue([]);
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));

    expect(screen.queryByTestId("hold-card-room-1")).not.toBeInTheDocument();
    fireEvent.click(await screen.findByText(/Set temporary hold/));

    expect(screen.getByTestId("hold-modal")).toBeInTheDocument();
    // Scoped to the room whose footer it was opened from.
    expect(screen.getByLabelText("Room")).toHaveValue("room-1");

    // Close puts the page back without any API call.
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByTestId("hold-modal")).not.toBeInTheDocument();
    expect(api.setOverride).not.toHaveBeenCalled();
  });

  it("opens the modal in replace mode from Manage hold", async () => {
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));

    const card = await screen.findByTestId("hold-card-room-1");
    fireEvent.click(within(card).getByRole("button", { name: "Manage hold" }));

    expect(screen.getByTestId("hold-modal")).toBeInTheDocument();
    // The shared modal sees the live hold, so it offers replace + cancel.
    expect(screen.getByText("Temporary hold active")).toBeInTheDocument();
    expect(screen.getByTestId("hold-modal-cancel-hold")).toBeInTheDocument();
  });
});
