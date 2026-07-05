import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import Dashboard from "./Dashboard";
import * as api from "../api";
import { SystemContext, DevModeContext } from "../contexts";

vi.mock("../api");

const mockSystem = { enabled: true, toggle: vi.fn().mockResolvedValue(undefined) };
const mockDevMode = { devMode: false, toggleDevMode: vi.fn().mockResolvedValue(undefined) };

const mockStatus: api.ZoneStatus[] = [
  {
    thermostat_entity_id: "climate.test",
    hvac_mode: "cool",
    hvac_action: "cooling",
    current_temp: 75.2,
    setpoint: 72.0,
    cycle_state: "running",
    cycle_id: "c1",
    cycle_started_at: "2024-01-01T12:00:00",
    rooms: [
      {
        room_id: "room-1",
        avg_temp: 76.1,
        presence_active: true,
        vent_states: { "cover.vent": "open" },
        target_temp: 72.0,
      },
    ],
  },
];

describe("Dashboard Page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getSensorHealth).mockResolvedValue({ stale_after_min: 30, rooms: [] });
    vi.mocked(api.getSensorStaleness).mockResolvedValue({ stale_after_min: 30 });
    vi.mocked(api.getThermostatHealth).mockResolvedValue({ thermostats: [] });
    vi.mocked(api.getStatus).mockResolvedValue(mockStatus);
    vi.mocked(api.connectWS).mockReturnValue(() => {});
    vi.mocked(api.getVacationMode).mockResolvedValue({ enabled: false, return_at: null });
    vi.mocked(api.getRooms).mockResolvedValue([
      {
        id: "room-1",
        name: "Living Room",
        thermostat_entity_id: "climate.test",
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
    ]);
    vi.mocked(api.getThermostats).mockResolvedValue([
      {
        thermostat_entity_id: "climate.test",
        name: "Main HVAC",
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
      },
    ]);
  });

  it("renders the dashboard with zone status", async () => {
    render(
      <SystemContext.Provider value={mockSystem}>
        <DevModeContext.Provider value={mockDevMode}>
          <Dashboard />
        </DevModeContext.Provider>
      </SystemContext.Provider>
    );

    expect(await screen.findByText(/Dashboard/i)).toBeInTheDocument();
    expect(await screen.findByText(/Main HVAC/i)).toBeInTheDocument();
    expect(screen.getByText("75.2°F")).toBeInTheDocument();
    expect(screen.getByText("72.0°F")).toBeInTheDocument();
    expect(screen.getByText("Living Room")).toBeInTheDocument();
  });

  it("shows the temperature an active room is requesting from the cycle", async () => {
    render(
      <SystemContext.Provider value={mockSystem}>
        <DevModeContext.Provider value={mockDevMode}>
          <Dashboard />
        </DevModeContext.Provider>
      </SystemContext.Provider>
    );
    // Room's live temp and the target it is requesting are both shown.
    expect(await screen.findByText("76.1°F")).toBeInTheDocument();
    expect(screen.getByText(/requesting 72.0°F/)).toBeInTheDocument();
  });

  it("clears presence for an active room from the dashboard", async () => {
    vi.mocked(api.clearPresenceHoldover).mockResolvedValue(undefined);
    render(
      <SystemContext.Provider value={mockSystem}>
        <DevModeContext.Provider value={mockDevMode}>
          <Dashboard />
        </DevModeContext.Provider>
      </SystemContext.Provider>
    );
    const clearBtn = await screen.findByRole("button", { name: /Clear presence/i });
    await act(async () => {
      fireEvent.click(clearBtn);
    });
    expect(api.clearPresenceHoldover).toHaveBeenCalledWith("room-1");
    // load() re-runs after clearing, so getStatus is called a second time.
    expect(api.getStatus).toHaveBeenCalledTimes(2);
  });

  it("renders one Clear-presence button per room for a multi-room cycle", async () => {
    // A cycle with three presence-active rooms must render three independent
    // Clear-presence buttons — one inside each room's row — not a single button
    // at the bottom of the section.
    vi.mocked(api.clearPresenceHoldover).mockResolvedValue(undefined);
    vi.mocked(api.getStatus).mockResolvedValue([
      {
        thermostat_entity_id: "climate.test",
        hvac_mode: "cool",
        hvac_action: "cooling",
        current_temp: 75.2,
        setpoint: 72.0,
        cycle_state: "running",
        cycle_id: "c1",
        cycle_started_at: "2024-01-01T12:00:00",
        rooms: [
          {
            room_id: "room-1",
            avg_temp: 76.1,
            presence_active: true,
            vent_states: { "cover.v1": "open" },
            target_temp: 72.0,
          },
          {
            room_id: "room-2",
            avg_temp: 70.0,
            presence_active: true,
            vent_states: { "cover.v2": "closed" },
            target_temp: 68.0,
          },
          {
            room_id: "room-3",
            avg_temp: 73.5,
            presence_active: true,
            vent_states: { "cover.v3": "open" },
            target_temp: 71.0,
          },
        ],
      },
    ]);
    vi.mocked(api.getRooms).mockResolvedValue(
      ["room-1", "room-2", "room-3"].map((id, i) => ({
        id,
        name: ["Living Room", "Bedroom", "Kitchen"][i],
        thermostat_entity_id: "climate.test",
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
      }))
    );

    render(
      <SystemContext.Provider value={mockSystem}>
        <DevModeContext.Provider value={mockDevMode}>
          <Dashboard />
        </DevModeContext.Provider>
      </SystemContext.Provider>
    );

    // Three rooms → three Clear-presence buttons (one per room).
    const buttons = await screen.findAllByRole("button", { name: /Clear presence/i });
    expect(buttons).toHaveLength(3);

    // Each room shows its own requesting temperature.
    expect(screen.getByText(/requesting 72.0°F/)).toBeInTheDocument();
    expect(screen.getByText(/requesting 68.0°F/)).toBeInTheDocument();
    expect(screen.getByText(/requesting 71.0°F/)).toBeInTheDocument();

    // Each button is bound to its own room: clicking the 2nd clears only room-2,
    // the 3rd clears only room-3.
    await act(async () => {
      fireEvent.click(buttons[1]);
    });
    expect(api.clearPresenceHoldover).toHaveBeenCalledWith("room-2");
    await act(async () => {
      fireEvent.click(buttons[2]);
    });
    expect(api.clearPresenceHoldover).toHaveBeenCalledWith("room-3");
    expect(api.clearPresenceHoldover).not.toHaveBeenCalledWith("room-1");
  });

  it("handles refresh", async () => {
    render(
      <SystemContext.Provider value={mockSystem}>
        <DevModeContext.Provider value={mockDevMode}>
          <Dashboard />
        </DevModeContext.Provider>
      </SystemContext.Provider>
    );

    const refreshBtn = await screen.findByText(/Refresh/i);
    // Refresh calls load(), which fires getStatus/getRooms/etc. and then runs
    // trailing setState calls (setZones/setRooms/setLastUpdate) once the
    // promises resolve. Click inside act(async) so those updates flush before
    // the test ends — otherwise they land after teardown and React warns.
    await act(async () => {
      fireEvent.click(refreshBtn);
    });
    expect(api.getStatus).toHaveBeenCalledTimes(2); // Initial + click
  });

  it("shows vacation mode active state and opens modal on button click", async () => {
    vi.mocked(api.getVacationMode).mockResolvedValue({
      enabled: true,
      return_at: "2026-12-25T10:00:00.000Z",
    });
    render(
      <SystemContext.Provider value={mockSystem}>
        <DevModeContext.Provider value={mockDevMode}>
          <Dashboard />
        </DevModeContext.Provider>
      </SystemContext.Provider>
    );
    expect(await screen.findByText(/Vacation mode active/i)).toBeInTheDocument();
    expect(screen.getByText(/Schedules paused until/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Vacation mode active/i }));
    expect(screen.getByText(/End vacation mode early/i)).toBeInTheDocument();
  });

  it("surfaces stale sensors as a top-of-Dashboard banner (Issue #211)", async () => {
    vi.mocked(api.getSensorHealth).mockResolvedValue({
      stale_after_min: 30,
      rooms: [
        {
          room_id: "r1",
          room_name: "Bedroom",
          thermostat_entity_id: "climate.test",
          stale_sensors: [{ entity_id: "sensor.bedroom_temp", age_seconds: 5400, reason: "stale" }],
        },
      ],
    });
    render(
      <SystemContext.Provider value={mockSystem}>
        <DevModeContext.Provider value={mockDevMode}>
          <Dashboard />
        </DevModeContext.Provider>
      </SystemContext.Provider>
    );
    const banner = await screen.findByTestId("stale-sensors-banner");
    expect(banner).toHaveTextContent("1 sensor not reporting");
    expect(banner).toHaveTextContent("Bedroom");
    expect(banner).toHaveTextContent("sensor.bedroom_temp");
    expect(banner).toHaveTextContent("1.5 h ago");
  });

  it("does not render the stale-sensors banner when everything is fresh", async () => {
    render(
      <SystemContext.Provider value={mockSystem}>
        <DevModeContext.Provider value={mockDevMode}>
          <Dashboard />
        </DevModeContext.Provider>
      </SystemContext.Provider>
    );
    await screen.findByText("Dashboard");
    expect(screen.queryByTestId("stale-sensors-banner")).not.toBeInTheDocument();
  });

  it("surfaces unavailable thermostats as a top-of-Dashboard banner (Issue #267)", async () => {
    vi.mocked(api.getThermostatHealth).mockResolvedValue({
      thermostats: [
        {
          thermostat_entity_id: "climate.test",
          name: "Main HVAC",
          reason: "unavailable",
          unavailable_seconds: 120,
          abort_after_min: 5,
          cycle_running: true,
        },
      ],
    });
    render(
      <SystemContext.Provider value={mockSystem}>
        <DevModeContext.Provider value={mockDevMode}>
          <Dashboard />
        </DevModeContext.Provider>
      </SystemContext.Provider>
    );
    const banner = await screen.findByTestId("unavailable-thermostats-banner");
    expect(banner).toHaveTextContent("1 thermostat unavailable in Home Assistant");
    expect(banner).toHaveTextContent("Main HVAC");
    expect(banner).toHaveTextContent("climate.test");
    expect(banner).toHaveTextContent("unavailable for 2 min");
    // With a cycle in flight, the banner says what the engine will do about it.
    expect(banner).toHaveTextContent("aborts after 5 min");
  });

  it("warns when the unavailability abort is disabled and a cycle is running", async () => {
    vi.mocked(api.getThermostatHealth).mockResolvedValue({
      thermostats: [
        {
          thermostat_entity_id: "climate.test",
          name: "Main HVAC",
          reason: "unavailable",
          unavailable_seconds: 600,
          abort_after_min: 0,
          cycle_running: true,
        },
      ],
    });
    render(
      <SystemContext.Provider value={mockSystem}>
        <DevModeContext.Provider value={mockDevMode}>
          <Dashboard />
        </DevModeContext.Provider>
      </SystemContext.Provider>
    );
    const banner = await screen.findByTestId("unavailable-thermostats-banner");
    expect(banner).toHaveTextContent("will NOT be auto-aborted");
  });

  it("does not render the unavailable-thermostats banner when all are reachable", async () => {
    render(
      <SystemContext.Provider value={mockSystem}>
        <DevModeContext.Provider value={mockDevMode}>
          <Dashboard />
        </DevModeContext.Provider>
      </SystemContext.Provider>
    );
    await screen.findByText("Dashboard");
    expect(screen.queryByTestId("unavailable-thermostats-banner")).not.toBeInTheDocument();
  });
});
