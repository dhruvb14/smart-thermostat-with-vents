import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
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
      },
    ],
  },
];

describe("Dashboard Page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
        notes: "",
        system_wide_temp: null,
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
        min_open_vents: 1,
        overshoot_delta: 0.5,
        cycle_timeout_hours: 2,
        reconciliation_interval_min: 5,
        vacation_hvac_mode: "single" as const,
        min_cycle_runtime_min: 0,
        min_cycle_offtime_min: 0,
        cooling_lockout_below_f: null,
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

  it("handles refresh", async () => {
    render(
      <SystemContext.Provider value={mockSystem}>
        <DevModeContext.Provider value={mockDevMode}>
          <Dashboard />
        </DevModeContext.Provider>
      </SystemContext.Provider>
    );

    const refreshBtn = await screen.findByText(/Refresh/i);
    fireEvent.click(refreshBtn);
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
});
