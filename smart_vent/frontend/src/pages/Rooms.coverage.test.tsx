/**
 * Rooms page — behavioural coverage for the branches the main suite leaves
 * open: the settings-form validation guards, the empty/absent-entity states,
 * the live-status permutations on the room card, and the two timers.
 *
 * Kept in its own file so `Rooms.test.tsx` stays the narrative suite; every
 * test here drives the public surface (render + user event) rather than
 * reaching into the module's private helpers.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within, act } from "@testing-library/react";
import Rooms from "./Rooms";
import * as api from "../api";
import { SystemContext, UnitContext, buildUnitContext } from "../contexts";
import { ecoThermostatDefaults, ecoRoomDefaults } from "../testFixtures";

vi.mock("../api");

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const makeThermostat = (over: Partial<api.ThermostatConfig> = {}): api.ThermostatConfig => ({
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
  ...over,
});

const makeRoom = (over: Partial<api.Room> = {}): api.Room =>
  ({
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
    ...over,
  }) as api.Room;

const makeStatus = (over: Partial<api.RoomActiveStatus> = {}): api.RoomActiveStatus => ({
  room_id: "room-1",
  source: "idle",
  target_temp: null,
  ends_in_seconds: null,
  override_respect_eco: null,
  presence_holdover_active: false,
  presence_suppressed: false,
  next_schedule_in_seconds: null,
  next_schedule_target: null,
  next_schedule_label: null,
  ...over,
});

const mockSystem = { enabled: true, toggle: async () => {} };
const offSystem = { enabled: false, toggle: async () => {} };

/** Point getRooms / getRoom at the given rooms. */
function useRooms(rooms: api.Room[]) {
  vi.mocked(api.getRooms).mockResolvedValue(rooms);
  vi.mocked(api.getRoom).mockImplementation((id: string) =>
    Promise.resolve(rooms.find((r) => r.id === id) as api.Room)
  );
}

function renderPage(system: typeof mockSystem = mockSystem, unit: "F" | "C" = "F") {
  return render(
    <UnitContext.Provider value={buildUnitContext(unit)}>
      <SystemContext.Provider value={system}>
        <Rooms />
      </SystemContext.Provider>
    </UnitContext.Provider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  useRooms([makeRoom()]);
  vi.mocked(api.getThermostats).mockResolvedValue([makeThermostat()]);
  vi.mocked(api.getRoomActiveStatuses).mockResolvedValue({ "room-1": makeStatus() });
  vi.mocked(api.getEntityStates).mockResolvedValue({});
  vi.mocked(api.getHAEntities).mockResolvedValue([]);
  vi.mocked(api.getOverrides).mockResolvedValue([]);
  vi.mocked(api.getSensorHealth).mockResolvedValue({ stale_after_min: 30, rooms: [] });
  vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
    entity_id: "sensor.outdoor",
    current_value: 80,
  });
});

// ---------------------------------------------------------------------------
// Room settings form — validation guards
// ---------------------------------------------------------------------------

describe("Rooms settings form — validation guards", () => {
  const openNewRoom = async () => {
    renderPage();
    fireEvent.click(await screen.findByText("+ Add room"));
    await screen.findByText("New Room", { selector: ".page-title" });
  };

  it("requires a thermostat before creating a room", async () => {
    await openNewRoom();
    fireEvent.change(screen.getByLabelText(/Room name/i), { target: { value: "Den" } });
    // The select starts on the "— select a thermostat —" empty option.
    fireEvent.click(screen.getByRole("button", { name: /Create room/i }));

    expect(await screen.findByText("Thermostat is required")).toBeInTheDocument();
    expect(api.createRoom).not.toHaveBeenCalled();
  });

  it("rejects a deadband override above the documented maximum", async () => {
    await openNewRoom();
    fireEvent.change(screen.getByLabelText(/Room name/i), { target: { value: "Den" } });
    fireEvent.change(screen.getByRole("combobox", { name: /Thermostat/i }), {
      target: { value: "climate.test" },
    });
    fireEvent.change(screen.getByLabelText(/Deadband override/i), { target: { value: "20" } });
    fireEvent.click(screen.getByRole("button", { name: /Create room/i }));

    expect(
      await screen.findByText("Deadband override must be between 0°F and 10°F")
    ).toBeInTheDocument();
    expect(api.createRoom).not.toHaveBeenCalled();
  });

  it("rejects a negative pre-cool/pre-heat minimum outside difference", async () => {
    await openNewRoom();
    fireEvent.change(screen.getByLabelText(/Room name/i), { target: { value: "Den" } });
    fireEvent.change(screen.getByRole("combobox", { name: /Thermostat/i }), {
      target: { value: "climate.test" },
    });
    const toggle = await screen.findByLabelText(/pre-cool \/ pre-heat/i);
    await waitFor(() => expect(toggle).not.toBeDisabled());
    fireEvent.click(toggle);

    fireEvent.change(screen.getByLabelText(/Minimum outside difference/i), {
      target: { value: "-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Create room/i }));

    expect(
      await screen.findByText("Pre-cool/pre-heat: minimum outside difference must be 0 or greater")
    ).toBeInTheDocument();
    expect(api.createRoom).not.toHaveBeenCalled();
  });

  it("reveals the schedule-window field in off_schedule_only mode and rejects a negative window", async () => {
    await openNewRoom();
    fireEvent.change(screen.getByLabelText(/Room name/i), { target: { value: "Den" } });
    fireEvent.change(screen.getByRole("combobox", { name: /Thermostat/i }), {
      target: { value: "climate.test" },
    });
    const toggle = await screen.findByLabelText(/pre-cool \/ pre-heat/i);
    await waitFor(() => expect(toggle).not.toBeDisabled());
    fireEvent.click(toggle);

    // "Any presence" hides the window field entirely.
    expect(screen.queryByLabelText(/Schedule window/i)).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/When to apply/i), {
      target: { value: "off_schedule_only" },
    });
    const windowInput = screen.getByLabelText(/Schedule window/i);
    expect(windowInput).toHaveValue(60);

    fireEvent.change(windowInput, { target: { value: "-5" } });
    fireEvent.click(screen.getByRole("button", { name: /Create room/i }));

    expect(
      await screen.findByText("Pre-cool/pre-heat: schedule window must be 0 or greater")
    ).toBeInTheDocument();
    expect(api.createRoom).not.toHaveBeenCalled();
  });

  it("submits an explicit 0 for every zeroed numeric field", async () => {
    // A thermostat with a 0 deadband is the only way a 0 widened deadband
    // passes the >= thermostat-deadband guard, which is what lets all four
    // `parseFloat(...) || 0` fallbacks be exercised in one save.
    vi.mocked(api.getThermostats).mockResolvedValue([makeThermostat({ deadband: 0 })]);
    vi.mocked(api.createRoom).mockResolvedValue(makeRoom({ id: "room-9", name: "Zeroes" }));

    await openNewRoom();
    fireEvent.change(screen.getByLabelText(/Room name/i), { target: { value: "Zeroes" } });
    fireEvent.change(screen.getByRole("combobox", { name: /Thermostat/i }), {
      target: { value: "climate.test" },
    });
    fireEvent.change(screen.getByLabelText(/Presence holdover/i), { target: { value: "0" } });

    const toggle = await screen.findByLabelText(/pre-cool \/ pre-heat/i);
    await waitFor(() => expect(toggle).not.toBeDisabled());
    fireEvent.click(toggle);
    fireEvent.change(screen.getByLabelText(/When to apply/i), {
      target: { value: "off_schedule_only" },
    });
    fireEvent.change(screen.getByLabelText(/Schedule window/i), { target: { value: "0" } });
    fireEvent.change(screen.getByLabelText(/Minimum outside difference/i), {
      target: { value: "0" },
    });
    fireEvent.change(screen.getByLabelText(/Widened deadband/i), { target: { value: "0" } });

    fireEvent.click(screen.getByRole("button", { name: /Create room/i }));

    await waitFor(() => expect(api.createRoom).toHaveBeenCalled());
    const [payload] = vi.mocked(api.createRoom).mock.calls[0];
    expect(payload).toMatchObject({
      presence_holdover_hours: 0,
      ambient_suppression_min_differential: 0,
      ambient_suppression_deadband: 0,
      ambient_suppression_off_schedule_window_min: 0,
      ambient_suppression_mode: "off_schedule_only",
    });
  });

  it("surfaces the API error message when the save is rejected", async () => {
    vi.mocked(api.createRoom).mockRejectedValue(new Error("Room name already taken"));
    await openNewRoom();
    fireEvent.change(screen.getByLabelText(/Room name/i), { target: { value: "Den" } });
    fireEvent.change(screen.getByRole("combobox", { name: /Thermostat/i }), {
      target: { value: "climate.test" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Create room/i }));

    expect(await screen.findByText("Room name already taken")).toBeInTheDocument();
    // The button is re-enabled by the finally block so the user can retry.
    expect(screen.getByRole("button", { name: /Create room/i })).not.toBeDisabled();
  });

  it("falls back to a generic message when the rejection is not an Error", async () => {
    vi.mocked(api.createRoom).mockRejectedValue("kaboom");
    await openNewRoom();
    fireEvent.change(screen.getByLabelText(/Room name/i), { target: { value: "Den" } });
    fireEvent.change(screen.getByRole("combobox", { name: /Thermostat/i }), {
      target: { value: "climate.test" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Create room/i }));

    expect(await screen.findByText("Save failed")).toBeInTheDocument();
  });

  it("points the user at the Thermostats page when none are registered", async () => {
    vi.mocked(api.getThermostats).mockResolvedValue([]);
    await openNewRoom();

    expect(screen.getByText(/No thermostats registered yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: /Thermostat/i })).not.toBeInTheDocument();
  });

  it("keeps pre-cool/pre-heat locked when the outside-sensor probe fails", async () => {
    vi.mocked(api.getOutsideTempEntity).mockRejectedValue(new Error("HA offline"));
    await openNewRoom();

    const toggle = await screen.findByLabelText(/pre-cool \/ pre-heat/i);
    expect(toggle).toBeDisabled();
    expect(screen.getByText(/Add an outside temperature sensor/i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Configure view — empty / absent entity lists
// ---------------------------------------------------------------------------

describe("Rooms configure view", () => {
  const openConfigure = async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Configure sensors/i }));
    await screen.findByTestId("room-configure");
  };

  it("renders empty hints, warnings and plural counts for a room with no entities", async () => {
    // `sensors` / `vents` / `presence_sensors` absent entirely — the API omits
    // them on some payloads, so the `?? []` fallbacks must hold.
    const bare = makeRoom();
    delete (bare as Partial<api.Room>).sensors;
    delete (bare as Partial<api.Room>).vents;
    delete (bare as Partial<api.Room>).presence_sensors;
    useRooms([bare]);

    await openConfigure();

    // Quick-status strip pluralises on 0.
    expect(screen.getByText("temp sensors")).toBeInTheDocument();
    expect(screen.getByText("vents")).toBeInTheDocument();
    expect(screen.getByText("presence sensors")).toBeInTheDocument();

    // Both warning cards.
    expect(
      screen.getByText(/No temperature sensors — this room will be skipped/i)
    ).toBeInTheDocument();
    expect(screen.getByText(/Sensor-only room — no vents configured/i)).toBeInTheDocument();

    // Empty hints from EntitySection / VentTable.
    expect(screen.getByText("No sensors added yet — search above to add one.")).toBeInTheDocument();
    expect(screen.getByText(/No vents added yet — search above to add one/i)).toBeInTheDocument();
    expect(
      screen.getByText("No presence sensors added — the room will only activate via schedules.")
    ).toBeInTheDocument();
  });

  it("adds a vent through the picker and re-reads the room afterwards", async () => {
    const withVent = makeRoom({
      vents: [
        { id: "v9", room_id: "room-1", entity_id: "cover.new_vent", control_method: "open_close" },
      ],
    });
    const without = makeRoom({ vents: [] });
    useRooms([without]);
    vi.mocked(api.getHAEntities).mockResolvedValue([
      { entity_id: "cover.new_vent", friendly_name: "New Vent", state: "" },
    ]);
    vi.mocked(api.addVent).mockImplementation(async () => {
      // After the add succeeds the configure view re-reads the room; return the
      // post-add shape so the refetch is observable in the DOM.
      vi.mocked(api.getRoom).mockResolvedValue(withVent);
      return { id: "v9", room_id: "room-1", entity_id: "cover.new_vent" } as never;
    });

    await openConfigure();
    const picker = screen.getByPlaceholderText(/Search vents/i);
    fireEvent.focus(picker);
    fireEvent.change(picker, { target: { value: "new" } });
    fireEvent.mouseDown(await screen.findByText("New Vent"));

    await waitFor(() => expect(api.addVent).toHaveBeenCalledWith("room-1", "cover.new_vent"));
    // The refresh landed: the vent table now lists the entity.
    expect(await screen.findByText("cover.new_vent")).toBeInTheDocument();
    expect(screen.queryByText(/No vents added yet/i)).not.toBeInTheDocument();
  });

  it("falls back to the raw entity id when the room's thermostat is not registered", async () => {
    useRooms([makeRoom({ thermostat_entity_id: "climate.ghost" })]);
    await openConfigure();

    const header = screen.getByTestId("room-configure");
    // No matching ThermostatConfig → the id stands in for the friendly name and
    // is therefore rendered twice (name slot + the parenthesised mono id).
    expect(within(header).getAllByText(/climate\.ghost/)).toHaveLength(2);
  });

  it("shows a positive temperature offset in the quick-status strip", async () => {
    useRooms([makeRoom({ temp_offset: 2 })]);
    await openConfigure();
    expect(screen.getByText("+2°F")).toBeInTheDocument();
  });

  it("shows a negative temperature offset without a plus sign", async () => {
    useRooms([makeRoom({ temp_offset: -1.5 })]);
    await openConfigure();
    expect(screen.getByText("-1.5°F")).toBeInTheDocument();
  });

  it("reports a failed vent test on the row", async () => {
    vi.mocked(api.testVent).mockRejectedValue(new Error("service not found"));
    await openConfigure();

    fireEvent.click(screen.getByText("Test open"));

    expect(await screen.findByText(/service not found/)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Room card — live status permutations
// ---------------------------------------------------------------------------

describe("Rooms card — live status", () => {
  it("renders a room with no sensors, vents or presence sensors", async () => {
    const bare = makeRoom();
    delete (bare as Partial<api.Room>).sensors;
    delete (bare as Partial<api.Room>).vents;
    delete (bare as Partial<api.Room>).presence_sensors;
    useRooms([bare]);

    const { container } = renderPage();
    await screen.findByText("Living Room");

    // No entity ids at all → the HA state fetch is skipped entirely.
    expect(api.getEntityStates).not.toHaveBeenCalled();
    expect(container.querySelector(".room-live-value")).toHaveTextContent("—");
    expect(screen.getByText(/🌡 0 sensors/)).toBeInTheDocument();
    expect(screen.getByText(/💨 0 vents/)).toBeInTheDocument();
    expect(screen.getByText(/🚶 0 presence/)).toBeInTheDocument();
    expect(screen.getByText(/No temperature sensors — configure below/)).toBeInTheDocument();
  });

  it("warns about missing vents when only sensors are configured", async () => {
    useRooms([makeRoom({ vents: [] })]);
    renderPage();
    await screen.findByText("Living Room");

    expect(screen.getByText(/⚠ No vents — configure below/)).toBeInTheDocument();
  });

  it("keeps rendering when the HA state fetch fails", async () => {
    vi.mocked(api.getEntityStates).mockRejectedValue(new Error("HA down"));
    const { container } = renderPage();
    await screen.findByText("Living Room");

    // Sensors exist but no state arrived → the ellipsis placeholder, not "—".
    await waitFor(() => expect(container.querySelector(".room-live-value")).toHaveTextContent("…"));
  });

  it("shows a loading status row until active-status data arrives", async () => {
    vi.mocked(api.getRoomActiveStatuses).mockResolvedValue({});
    const { container } = renderPage();
    await screen.findByText("Living Room");

    expect(container.querySelector(".room-status-loading")).toHaveTextContent("…");
    expect(screen.queryByText("Not active")).not.toBeInTheDocument();
  });

  it("marks the card as disabled when the system is globally off", async () => {
    const { container } = renderPage(offSystem);
    await screen.findByText("Living Room");

    expect(container.querySelector(".card")).toHaveClass("room-card-disabled");
    expect(screen.getByText("⏸ Global Off")).toBeInTheDocument();
  });

  it("labels a schedule-driven room and counts down to the next block", async () => {
    vi.mocked(api.getRoomActiveStatuses).mockResolvedValue({
      "room-1": makeStatus({
        source: "schedule",
        target_temp: 70,
        ends_in_seconds: 1800,
        next_schedule_in_seconds: 3600,
        next_schedule_target: 68,
        next_schedule_label: "Evening",
      }),
    });
    const { container } = renderPage();

    expect(await screen.findByText("via Schedule")).toBeInTheDocument();
    expect(screen.getByText("🎯 70.0°F")).toBeInTheDocument();
    // Active room → "then", and the countdown renders because nextIn > 0.
    expect(screen.getByText(/then/)).toBeInTheDocument();
    expect(screen.getByText("68.0°F")).toBeInTheDocument();
    // The countdown span renders alongside the label because nextIn > 0. Its
    // exact seconds depend on the render clock, so pin the shape, not the tick
    // (the fake-timer suite below pins the exact string).
    expect(container.querySelector(".room-status-next-timer")?.textContent).toMatch(/^ \(\d+[hms]/);
  });

  it("shows the next block without a timer for an idle room whose block is due now", async () => {
    vi.mocked(api.getRoomActiveStatuses).mockResolvedValue({
      "room-1": makeStatus({
        next_schedule_in_seconds: 0,
        next_schedule_target: 68,
        next_schedule_label: "Morning",
      }),
    });
    const { container } = renderPage();

    expect(await screen.findByText("Not active")).toBeInTheDocument();
    // Idle room → "next" rather than "then"; nextIn === 0 → no timer span.
    expect(screen.getByText(/next/)).toBeInTheDocument();
    expect(container.querySelector(".room-status-next-timer")).toBeNull();
  });

  it("renders an em dash for a status source the UI does not know", async () => {
    vi.mocked(api.getRoomActiveStatuses).mockResolvedValue({
      "room-1": makeStatus({
        source: "manual" as unknown as api.RoomActiveStatus["source"],
        target_temp: 71,
      }),
    });
    renderPage();

    expect(await screen.findByText("via —")).toBeInTheDocument();
  });

  it("shows the bare entity id when the card's thermostat is unregistered", async () => {
    useRooms([makeRoom({ thermostat_entity_id: "climate.ghost" })]);
    const { container } = renderPage();
    await screen.findByText("Living Room");

    const mono = container.querySelectorAll(".font-mono");
    expect(mono[0]).toHaveTextContent("climate.ghost");
    expect(screen.queryByText(/Test Thermostat/)).not.toBeInTheDocument();
  });

  it("shows Occupied plus the holdover countdown when presence is the active source", async () => {
    useRooms([
      makeRoom({
        presence_sensors: [{ id: "p1", room_id: "room-1", entity_id: "binary_sensor.motion" }],
      }),
    ]);
    vi.mocked(api.getEntityStates).mockResolvedValue({
      "sensor.temp": { state: "70", numeric: 70, unit: "°F", attributes: {} },
      "binary_sensor.motion": { state: "on", numeric: null, unit: "", attributes: {} },
    });
    vi.mocked(api.getRoomActiveStatuses).mockResolvedValue({
      "room-1": makeStatus({ source: "presence", target_temp: 72, ends_in_seconds: 600 }),
    });
    const { container } = renderPage();

    expect(await screen.findByText(/Occupied/)).toBeInTheDocument();
    expect(container.querySelector(".live-occupied")).not.toBeNull();
    expect(container.querySelector(".room-status-next-timer")?.textContent).toMatch(
      /resets in \d+m \d+s/
    );
  });

  it("shows Unoccupied when the presence sensor reads off", async () => {
    useRooms([
      makeRoom({
        presence_sensors: [{ id: "p1", room_id: "room-1", entity_id: "binary_sensor.motion" }],
      }),
    ]);
    vi.mocked(api.getEntityStates).mockResolvedValue({
      "binary_sensor.motion": { state: "off", numeric: null, unit: "", attributes: {} },
    });
    const { container } = renderPage();

    expect(await screen.findByText("Unoccupied")).toBeInTheDocument();
    expect(container.querySelector(".live-unoccupied")).not.toBeNull();
  });

  it("shows an ellipsis until presence states arrive", async () => {
    useRooms([
      makeRoom({
        presence_sensors: [{ id: "p1", room_id: "room-1", entity_id: "binary_sensor.motion" }],
      }),
    ]);
    // Sensor state present, presence state missing → hasPresenceData is false.
    vi.mocked(api.getEntityStates).mockResolvedValue({
      "sensor.temp": { state: "70", numeric: 70, unit: "°F", attributes: {} },
    });
    const { container } = renderPage();
    await screen.findByText("Living Room");

    await waitFor(() => {
      const presenceValue = container.querySelectorAll(".room-live-value")[1];
      expect(presenceValue).toHaveTextContent("…");
      expect(presenceValue.className).toBe("room-live-value ");
    });
  });

  it("renders every vent-position flavour in the live strip", async () => {
    useRooms([
      makeRoom({
        vents: [
          { id: "v0", room_id: "room-1", entity_id: "cover.wide", control_method: "open_close" },
          { id: "v1", room_id: "room-1", entity_id: "cover.tilt", control_method: "open_close" },
          { id: "v2", room_id: "room-1", entity_id: "cover.half", control_method: "open_close" },
          { id: "v3", room_id: "room-1", entity_id: "cover.plain", control_method: "open_close" },
          { id: "v4", room_id: "room-1", entity_id: "cover.shut", control_method: "open_close" },
          { id: "v5", room_id: "room-1", entity_id: "cover.weird", control_method: "open_close" },
        ],
      }),
    ]);
    vi.mocked(api.getEntityStates).mockResolvedValue({
      // A fully-open standard cover, a Flair-style tilt at 0, and a mid
      // position → "Open", "Closed" and a percentage respectively.
      "cover.wide": {
        state: "closed",
        numeric: null,
        unit: "",
        attributes: { current_position: 100 },
      },
      "cover.tilt": {
        state: "open",
        numeric: null,
        unit: "",
        attributes: { current_tilt_position: 0 },
      },
      "cover.half": {
        state: "open",
        numeric: null,
        unit: "",
        attributes: { current_position: 55 },
      },
      // No position attributes at all → fall back to the cover state string.
      "cover.plain": { state: "open", numeric: null, unit: "", attributes: {} },
      "cover.shut": { state: "closed", numeric: null, unit: "", attributes: {} },
      "cover.weird": { state: "unavailable", numeric: null, unit: "", attributes: {} },
    });
    const { container } = renderPage();
    await screen.findByText("Living Room");

    await waitFor(() => {
      const pills = Array.from(container.querySelectorAll(".room-vent-pill")).map(
        (p) => p.textContent
      );
      // "cover.wide" reads Open from its position even though its state string
      // says closed — the position attribute wins over the state fallback.
      expect(pills).toEqual(["Open", "Closed", "55%", "Open", "Closed", "unavailable"]);
    });
  });

  it("shows a temperature-offset badge with the sign", async () => {
    useRooms([makeRoom({ temp_offset: 3 })]);
    renderPage();
    expect(await screen.findByText(/offset \+3°F/)).toBeInTheDocument();
  });

  it("omits the plus sign for a negative offset badge", async () => {
    useRooms([makeRoom({ temp_offset: -2 })]);
    renderPage();
    expect(await screen.findByText(/offset -2°F/)).toBeInTheDocument();
  });

  it("keeps the stale badge singular for exactly one sensor", async () => {
    vi.mocked(api.getSensorHealth).mockResolvedValue({
      stale_after_min: 30,
      rooms: [
        {
          room_id: "room-1",
          room_name: "Living Room",
          thermostat_entity_id: "climate.test",
          stale_sensors: [{ entity_id: "sensor.one", age_seconds: 5400, reason: "stale" }],
        },
      ],
    });
    renderPage();

    const badge = await screen.findByTestId("stale-badge-room-1");
    expect(badge.textContent).toBe("⚠ 1 stale sensor");
    expect(badge.getAttribute("title")).toBe("sensor.one — 1.5 h ago");
  });

  it("pluralises the stale badge and formats every staleness age", async () => {
    vi.mocked(api.getSensorHealth).mockResolvedValue({
      stale_after_min: 30,
      rooms: [
        {
          room_id: "room-1",
          room_name: "Living Room",
          thermostat_entity_id: "climate.test",
          stale_sensors: [
            { entity_id: "sensor.never", age_seconds: null, reason: "not_in_cache" },
            { entity_id: "sensor.null_age", age_seconds: null, reason: "stale" },
            { entity_id: "sensor.minutes", age_seconds: 120, reason: "stale" },
            { entity_id: "sensor.days", age_seconds: 172800, reason: "stale" },
          ],
        },
      ],
    });
    renderPage();

    const badge = await screen.findByTestId("stale-badge-room-1");
    // Exact text, not a substring: a hardcoded "s" would still satisfy
    // toHaveTextContent("… stale sensor") and leave the plural unpinned.
    expect(badge.textContent).toBe("⚠ 4 stale sensors");
    const title = badge.getAttribute("title") ?? "";
    expect(title).toContain("sensor.never — never seen by HA");
    expect(title).toContain("sensor.null_age — never seen by HA");
    expect(title).toContain("sensor.minutes — 2 min ago");
    expect(title).toContain("sensor.days — 2 d ago");
  });
});

// ---------------------------------------------------------------------------
// Timers
// ---------------------------------------------------------------------------

describe("Rooms — timers", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  /** Flush the mount promise chain without relying on waitFor's timers. */
  const flush = async () => {
    for (let i = 0; i < 10; i++) {
      await act(async () => {});
    }
  };

  it("ticks the ends-in countdown once a second", async () => {
    vi.mocked(api.getRoomActiveStatuses).mockResolvedValue({
      "room-1": makeStatus({ source: "schedule", target_temp: 70, ends_in_seconds: 125 }),
    });
    const { container } = renderPage();
    await flush();

    expect(container.querySelector(".room-status-ends")).toHaveTextContent("2m 5s");

    // The per-second interval re-renders the card; elapsed time is derived from
    // the clock, so the countdown must decrement without a refetch.
    await act(async () => {
      vi.advanceTimersByTime(3000);
    });
    expect(container.querySelector(".room-status-ends")).toHaveTextContent("2m 2s");
    expect(api.getRoomActiveStatuses).toHaveBeenCalledTimes(1);
  });

  it("re-polls statuses, holds and sensor health every 30 seconds", async () => {
    renderPage();
    await flush();

    expect(api.getRoomActiveStatuses).toHaveBeenCalledTimes(1);
    expect(api.getOverrides).toHaveBeenCalledTimes(1);
    expect(api.getSensorHealth).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(30_000);
    });
    await flush();

    expect(api.getRoomActiveStatuses).toHaveBeenCalledTimes(2);
    expect(api.getOverrides).toHaveBeenCalledTimes(2);
    expect(api.getSensorHealth).toHaveBeenCalledTimes(2);
  });
});

// ---------------------------------------------------------------------------
// Page-level edge cases
// ---------------------------------------------------------------------------

describe("Rooms — page edge cases", () => {
  it("renders the empty state and skips the status poll when there are no rooms", async () => {
    useRooms([]);
    renderPage();

    expect(await screen.findByText("No rooms yet.")).toBeInTheDocument();
    // Plural subtitle, and the "click Configure…" hint is suppressed at zero.
    expect(screen.getByText("0 rooms")).toBeInTheDocument();
    expect(screen.queryByText(/click "Configure sensors & vents"/)).not.toBeInTheDocument();
    // No rooms → no active-status request is issued at all.
    expect(api.getRoomActiveStatuses).not.toHaveBeenCalled();
  });

  it("cancels the outside-sensor probe when the settings form unmounts first", async () => {
    // The probe's `cancelled` flag exists so a late resolve cannot write into a
    // torn-down form. Leave it pending, unmount via Cancel, then resolve.
    let resolveProbe: (v: {
      entity_id: string | null;
      current_value: number | null;
    }) => void = () => {};
    vi.mocked(api.getOutsideTempEntity).mockReturnValue(
      new Promise((res) => {
        resolveProbe = res;
      })
    );

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Settings/i }));
    await screen.findByTestId("room-settings");

    fireEvent.click(screen.getByRole("button", { name: /^Cancel$/ }));
    await waitFor(() => expect(screen.queryByTestId("room-settings")).not.toBeInTheDocument());

    await act(async () => {
      resolveProbe({ entity_id: "sensor.outdoor", current_value: 80 });
    });

    // The list view is intact and a fresh mount still picks the probe up.
    expect(screen.getByText("Living Room")).toBeInTheDocument();
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: "sensor.outdoor",
      current_value: 80,
    });
    fireEvent.click(screen.getByRole("button", { name: /Settings/i }));
    const toggle = await screen.findByLabelText(/pre-cool \/ pre-heat/i);
    await waitFor(() => expect(toggle).not.toBeDisabled());
  });
});

describe("Rooms configure view — refresh after a mutation", () => {
  it("re-reads the room after adding a sensor so the tag list reflects the server", async () => {
    const before = makeRoom({ sensors: [] });
    const after = makeRoom({
      sensors: [{ id: "s2", room_id: "room-1", entity_id: "sensor.new_temp" }],
    });
    useRooms([before]);
    vi.mocked(api.getHAEntities).mockResolvedValue([
      { entity_id: "sensor.new_temp", friendly_name: "New Temp", state: "" },
    ]);
    vi.mocked(api.addSensor).mockImplementation(async () => {
      vi.mocked(api.getRoom).mockResolvedValue(after);
      return { id: "s2", room_id: "room-1", entity_id: "sensor.new_temp" };
    });

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Configure sensors/i }));
    await screen.findByTestId("room-configure");
    expect(screen.getByText("No sensors added yet — search above to add one.")).toBeInTheDocument();

    const picker = screen.getByPlaceholderText(/Search temperature sensors/i);
    fireEvent.focus(picker);
    fireEvent.change(picker, { target: { value: "new" } });
    fireEvent.mouseDown(await screen.findByText("New Temp"));

    await waitFor(() => expect(api.addSensor).toHaveBeenCalledWith("room-1", "sensor.new_temp"));
    // Only a real refetch can put the server's sensor into the tag list.
    expect(await screen.findByText("sensor.new_temp")).toBeInTheDocument();
    expect(screen.queryByText("No sensors added yet — search above to add one.")).toBeNull();
  });
});
