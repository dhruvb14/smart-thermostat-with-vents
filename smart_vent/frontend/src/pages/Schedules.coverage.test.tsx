/**
 * Behavioural coverage for the branches of `Schedules.tsx` that the main suite
 * never reaches: the day picker, every validation refusal in the schedule
 * modal, the expiry round-trip, the copy modal (including its refusals and its
 * result rendering), and the two failure paths that surface a backend error.
 *
 * Everything here drives the real rendered page — no private helper is called
 * directly — so a branch counted as covered is a branch a user can actually
 * reach.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { ecoRoomDefaults, makeHold } from "../testFixtures";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import Schedules from "./Schedules";
import * as api from "../api";

vi.mock("../api");

const makeRoom = (over: Partial<api.Room> = {}): api.Room => ({
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
  ...over,
});

const makeSchedule = (over: Partial<api.Schedule> = {}): api.Schedule => ({
  id: "sched-1",
  room_id: "room-1",
  days_of_week: [0, 1, 2, 3, 4],
  start_time: "22:00",
  end_time: "07:00",
  target_temp: 68,
  enabled: true,
  expires_at: null,
  ...over,
});

const created = makeSchedule({ id: "sched-new" });

/** The seven weekday buttons of the modal's day picker, Mon-first. */
const dayButtons = () =>
  Array.from(document.querySelectorAll<HTMLButtonElement>(".day-picker button"));

/** Expand the room card and open the "New Schedule" modal. */
const openNewBlock = async () => {
  fireEvent.click(await screen.findByText("Living Room"));
  fireEvent.click(await screen.findByText("+ Add schedule block"));
  // Move off the 22:00–07:00 default so the seeded block is not an overlap.
  fireEvent.change(screen.getByLabelText(/Start time/i), { target: { value: "10:00" } });
  fireEvent.change(screen.getByLabelText(/End time/i), { target: { value: "12:00" } });
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getRooms).mockResolvedValue([makeRoom()]);
  vi.mocked(api.getSchedules).mockResolvedValue([makeSchedule()]);
  vi.mocked(api.getOverrides).mockResolvedValue([]);
  vi.mocked(api.createSchedule).mockResolvedValue(created);
  vi.mocked(api.updateSchedule).mockResolvedValue(created);
});

describe("Schedules — day picker", () => {
  it("removes a selected day and adds an unselected one, keeping the list sorted", async () => {
    render(<Schedules />);
    await openNewBlock();

    // Default selection is Mon–Fri.
    expect(screen.getByText("Mon, Tue, Wed, Thu, Fri")).toBeInTheDocument();

    // Monday is selected → clicking it deselects.
    fireEvent.click(dayButtons()[0]);
    expect(screen.getByText("Tue, Wed, Thu, Fri")).toBeInTheDocument();
    expect(dayButtons()[0].className).not.toContain("selected");

    // Saturday is not selected → clicking it selects, and the payload stays in
    // ascending order rather than in click order.
    fireEvent.click(dayButtons()[5]);
    expect(screen.getByText("Tue, Wed, Thu, Fri, Sat")).toBeInTheDocument();
    expect(dayButtons()[5].className).toContain("selected");

    fireEvent.click(screen.getByText("Save"));
    await waitFor(() =>
      expect(api.createSchedule).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({ days_of_week: [1, 2, 3, 4, 5] })
      )
    );
  });

  it("says 'None selected' and refuses to save with no days", async () => {
    render(<Schedules />);
    await openNewBlock();

    [0, 1, 2, 3, 4].forEach((i) => fireEvent.click(dayButtons()[i]));
    expect(screen.getByText("None selected")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Save"));
    expect(await screen.findByText("Select at least one day")).toBeInTheDocument();
    expect(api.createSchedule).not.toHaveBeenCalled();
  });
});

describe("Schedules — modal validation refusals", () => {
  it("refuses to save with the target temperature cleared", async () => {
    render(<Schedules />);
    await openNewBlock();

    fireEvent.change(screen.getByLabelText(/Target temperature/i), { target: { value: "" } });
    fireEvent.click(screen.getByText("Save"));

    expect(await screen.findByText("Temperature required")).toBeInTheDocument();
    expect(api.createSchedule).not.toHaveBeenCalled();
  });

  it("refuses 'Auto-disable at' with no datetime, and saves once switched back to Never", async () => {
    render(<Schedules />);
    await openNewBlock();

    fireEvent.click(screen.getByLabelText("Auto-disable at"));
    expect(screen.getByLabelText("Auto-disable date and time")).toHaveValue("");
    fireEvent.click(screen.getByText("Save"));

    expect(
      await screen.findByText("Pick an auto-disable date and time, or choose Never expire")
    ).toBeInTheDocument();
    expect(api.createSchedule).not.toHaveBeenCalled();

    // The message names the escape hatch; taking it must actually work — and
    // the datetime input disappears with it.
    fireEvent.click(screen.getByLabelText("Never expire"));
    expect(screen.queryByLabelText("Auto-disable date and time")).toBeNull();
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() =>
      expect(api.createSchedule).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({ expires_at: null })
      )
    );
  });
});

describe("Schedules — expiry round-trip (#359)", () => {
  it("shows a stored expiry in the row and re-opens the editor already in 'at' mode", async () => {
    vi.mocked(api.getSchedules).mockResolvedValue([
      makeSchedule({ id: "sched-exp", expires_at: "2026-07-01T22:00:00" }),
    ]);
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));

    // The naive ISO string is rendered compactly — no "T", no seconds.
    expect(await screen.findByText("2026-07-01 22:00")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Edit"));
    expect(screen.getByLabelText("Auto-disable at")).toBeChecked();
    expect(screen.getByLabelText("Never expire")).not.toBeChecked();
    // datetime-local wants exactly "YYYY-MM-DDTHH:MM" — the seconds are sliced.
    expect(screen.getByLabelText("Auto-disable date and time")).toHaveValue("2026-07-01T22:00");

    // …and re-saving carries the same instant straight back.
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() =>
      expect(api.updateSchedule).toHaveBeenCalledWith(
        "room-1",
        "sched-exp",
        expect.objectContaining({ expires_at: "2026-07-01T22:00" })
      )
    );
  });

  it("renders 'Never' for a block with no expiry", async () => {
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));
    expect(await screen.findByText("Never")).toBeInTheDocument();
  });
});

describe("Schedules — save failures surface the backend message", () => {
  it("shows the Error message the API rejected with", async () => {
    vi.mocked(api.createSchedule).mockRejectedValue(new Error("Room is not configured"));
    render(<Schedules />);
    await openNewBlock();
    fireEvent.click(screen.getByText("Save"));

    expect(await screen.findByText("Room is not configured")).toBeInTheDocument();
    // The modal stays open so the user can correct and retry.
    expect(screen.getByText("New Schedule")).toBeInTheDocument();
    // …and the Save button is re-enabled by the finally block.
    expect(screen.getByText("Save")).not.toBeDisabled();
  });

  it("falls back to a generic message when the rejection is not an Error", async () => {
    vi.mocked(api.createSchedule).mockRejectedValue("socket hang up");
    render(<Schedules />);
    await openNewBlock();
    fireEvent.click(screen.getByText("Save"));

    expect(await screen.findByText("Save failed")).toBeInTheDocument();
  });
});

describe("Schedules — closing the schedule modal", () => {
  it("closes on Cancel", async () => {
    render(<Schedules />);
    await openNewBlock();
    expect(screen.getByText("New Schedule")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText("New Schedule")).toBeNull();
  });

  it("closes on a backdrop click but not on a click inside the modal", async () => {
    render(<Schedules />);
    await openNewBlock();

    // A click that bubbles up from inside the dialog must NOT dismiss it —
    // otherwise every field interaction would close the editor.
    fireEvent.click(screen.getByText("New Schedule"));
    expect(screen.getByText("New Schedule")).toBeInTheDocument();

    fireEvent.click(document.querySelector(".modal-backdrop") as HTMLElement);
    expect(screen.queryByText("New Schedule")).toBeNull();
  });
});

describe("Schedules — toggling a block", () => {
  it("reports a non-Error rejection with a generic message", async () => {
    vi.mocked(api.updateSchedule).mockRejectedValue({ code: 500 });
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("Disable"));

    expect(await screen.findByText("Could not change schedule status")).toBeInTheDocument();
    // The row is untouched — the toggle never optimistically flipped.
    expect(screen.getByText("Disable")).toBeInTheDocument();
  });
});

describe("Schedules — copy modal", () => {
  const bedroom = makeRoom({ id: "room-2", name: "Bedroom" });
  const kitchen = makeRoom({ id: "room-3", name: "Kitchen" });

  const openCopy = async () => {
    fireEvent.click(await screen.findByText("Living Room"));
    fireEvent.click(await screen.findByText("Copy"));
    return await screen.findByText("Copy schedule to other rooms");
  };

  it("says there is nowhere to copy to when the room is the only one", async () => {
    render(<Schedules />);
    await openCopy();

    expect(screen.getByText("No other rooms to copy to.")).toBeInTheDocument();
    // Nothing to select, so the action is disabled rather than erroring.
    const footerCopy = screen.getAllByRole("button", { name: "Copy" }).at(-1)!;
    expect(footerCopy).toBeDisabled();
  });

  it("refuses to copy with no target selected", async () => {
    vi.mocked(api.getRooms).mockResolvedValue([makeRoom(), bedroom]);
    render(<Schedules />);
    await openCopy();

    fireEvent.click(screen.getAllByRole("button", { name: "Copy" }).at(-1)!);

    expect(await screen.findByText("Select at least one room")).toBeInTheDocument();
    expect(api.copySchedule).not.toHaveBeenCalled();
  });

  it("selects and de-selects targets, copying only the ones still ticked", async () => {
    vi.mocked(api.getRooms).mockResolvedValue([makeRoom(), bedroom, kitchen]);
    vi.mocked(api.copySchedule).mockResolvedValue([
      { room_id: "room-3", schedule_id: "copy-1", status: "created", conflict_with: null },
    ]);
    render(<Schedules />);
    await openCopy();

    const bedroomBox = screen.getByLabelText("Bedroom") as HTMLInputElement;
    const kitchenBox = screen.getByLabelText("Kitchen") as HTMLInputElement;
    fireEvent.click(bedroomBox);
    fireEvent.click(kitchenBox);
    expect(bedroomBox.checked).toBe(true);
    expect(kitchenBox.checked).toBe(true);

    // Un-tick Bedroom again — the toggle's remove branch.
    fireEvent.click(bedroomBox);
    expect(bedroomBox.checked).toBe(false);

    fireEvent.click(screen.getAllByRole("button", { name: "Copy" }).at(-1)!);
    await waitFor(() =>
      expect(api.copySchedule).toHaveBeenCalledWith("room-1", "sched-1", ["room-3"])
    );
  });

  it("surfaces an Error rejection from the copy", async () => {
    vi.mocked(api.getRooms).mockResolvedValue([makeRoom(), bedroom]);
    vi.mocked(api.copySchedule).mockRejectedValue(new Error("Target room vanished"));
    render(<Schedules />);
    await openCopy();

    fireEvent.click(screen.getByLabelText("Bedroom"));
    fireEvent.click(screen.getAllByRole("button", { name: "Copy" }).at(-1)!);

    expect(await screen.findByText("Target room vanished")).toBeInTheDocument();
    expect(screen.getByText("Copy schedule to other rooms")).toBeInTheDocument();
  });

  it("falls back to a generic message when the copy rejects with a non-Error", async () => {
    vi.mocked(api.getRooms).mockResolvedValue([makeRoom(), bedroom]);
    vi.mocked(api.copySchedule).mockRejectedValue("boom");
    render(<Schedules />);
    await openCopy();

    fireEvent.click(screen.getByLabelText("Bedroom"));
    fireEvent.click(screen.getAllByRole("button", { name: "Copy" }).at(-1)!);

    expect(await screen.findByText("Copy failed")).toBeInTheDocument();
  });

  it("closes on Cancel and on a backdrop click", async () => {
    vi.mocked(api.getRooms).mockResolvedValue([makeRoom(), bedroom]);
    render(<Schedules />);
    await openCopy();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText("Copy schedule to other rooms")).toBeNull();

    fireEvent.click(screen.getByText("Copy"));
    await screen.findByText("Copy schedule to other rooms");
    fireEvent.click(document.querySelector(".modal-backdrop") as HTMLElement);
    expect(screen.queryByText("Copy schedule to other rooms")).toBeNull();
    expect(api.copySchedule).not.toHaveBeenCalled();
  });

  it("reports a conflicted copy, names the conflict, and falls back to the raw room id", async () => {
    vi.mocked(api.getRooms).mockResolvedValue([makeRoom(), bedroom]);
    vi.mocked(api.copySchedule).mockResolvedValue([
      {
        room_id: "room-2",
        schedule_id: "copy-1",
        status: "created_disabled_conflict",
        conflict_with: "Mon 22:00–07:00",
      },
      // A room the page never loaded (deleted between fetches): the result row
      // still has to identify itself, so it falls back to the id.
      { room_id: "room-gone", schedule_id: "copy-2", status: "created", conflict_with: null },
    ]);
    render(<Schedules />);
    await openCopy();

    fireEvent.click(screen.getByLabelText("Bedroom"));
    fireEvent.click(screen.getAllByRole("button", { name: "Copy" }).at(-1)!);

    const conflicted = await screen.findByText(/Copied \(disabled\)/);
    const conflictRow = conflicted.parentElement as HTMLElement;
    expect(conflictRow).toHaveTextContent("Bedroom");
    expect(conflictRow).toHaveTextContent("conflicts with Mon 22:00–07:00");

    const okRow = screen.getByText("Copied").parentElement as HTMLElement;
    expect(okRow).toHaveTextContent("room-gone");
    expect(okRow).not.toHaveTextContent("conflicts with");

    // The results panel is dismissible, and dismissing leaves the block list.
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("Copy results")).toBeNull();
    expect(screen.getByText("Edit")).toBeInTheDocument();
  });
});

describe("Schedules — cancelling a temporary hold (#576)", () => {
  it("falls back to a generic message when the cancel rejects with a non-Error", async () => {
    vi.mocked(api.getOverrides).mockResolvedValue([makeHold()]);
    vi.mocked(api.clearOverride).mockRejectedValue("gateway timeout");
    render(<Schedules />);
    fireEvent.click(await screen.findByText("Living Room"));

    const card = await screen.findByTestId("hold-card-room-1");
    fireEvent.click(within(card).getByRole("button", { name: "Cancel hold" }));

    expect(await screen.findByText("Could not cancel the hold")).toBeInTheDocument();
    // The hold card survives the failure — nothing was optimistically dropped.
    expect(screen.getByTestId("hold-card-room-1")).toBeInTheDocument();
  });
});

describe("Schedules — empty state", () => {
  it("points at the Rooms page when no rooms exist", async () => {
    vi.mocked(api.getRooms).mockResolvedValue([]);
    render(<Schedules />);

    expect(await screen.findByText(/No rooms configured yet/)).toBeInTheDocument();
    expect(api.getSchedules).not.toHaveBeenCalled();
  });

  it("still renders the page when the holds fetch fails", async () => {
    // Holds are supplementary — a failed GET /api/overrides must not take the
    // schedule list down with it.
    vi.mocked(api.getOverrides).mockRejectedValue(new Error("offline"));
    render(<Schedules />);

    fireEvent.click(await screen.findByText("Living Room"));
    expect(await screen.findByText("Edit")).toBeInTheDocument();
    const card = screen.getByText("Living Room").closest(".card") as HTMLElement;
    expect(within(card).queryByTestId("hold-card-room-1")).toBeNull();
  });
});
