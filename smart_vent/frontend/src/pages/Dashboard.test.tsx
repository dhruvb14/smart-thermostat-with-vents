import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import Dashboard from "./Dashboard";
import * as api from "../api";
import { SystemContext, DevModeContext } from "../contexts";
import React from "react";

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
    rooms: [
      {
        room_id: "room-1",
        avg_temp: 76.1,
        target_temp: 72.0,
        presence_active: true,
        vent_states: { "cover.vent": "open" }
      }
    ]
  }
];

describe("Dashboard Page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.getStatus as any).mockResolvedValue(mockStatus);
    (api.connectWS as any).mockReturnValue(() => {});
    (api.getRooms as any).mockResolvedValue([{ id: "room-1", name: "Living Room", thermostat_entity_id: "climate.test" }]);
    (api.getThermostats as any).mockResolvedValue([{ thermostat_entity_id: "climate.test", name: "Main HVAC" }]);
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
});
