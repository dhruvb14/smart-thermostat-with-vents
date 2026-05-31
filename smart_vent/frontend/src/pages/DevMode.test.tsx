import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import DevMode from "./DevMode";
import * as api from "../api";

import { DevModeContext } from "../contexts";

vi.mock("../api");

const mockDevLogs: api.EventLogEntry[] = [
  {
    id: 1,
    timestamp: "2024-01-01T12:00:00",
    message: "Vent opened",
    level: "info",
    category: "dev",
    details: { action: "open_vent", entity_id: "cover.living_room" },
  },
  {
    id: 2,
    timestamp: "2024-01-01T12:01:00",
    message: "Vent closed",
    level: "info",
    category: "dev",
    details: { action: "close_vent", entity_id: "cover.bedroom" },
  },
  {
    id: 3,
    timestamp: "2024-01-01T12:02:00",
    message: "Setpoint set",
    level: "info",
    category: "dev",
    details: { action: "set_thermostat", temperature: 72 },
  },
  {
    id: 4,
    timestamp: "2024-01-01T12:03:00",
    message: "Other action",
    level: "info",
    category: "dev",
    details: null,
  },
];

const mockZones: api.ZoneStatus[] = [
  {
    thermostat_entity_id: "climate.main",
    cycle_state: "running",
    hvac_mode: "cool",
    hvac_action: "cooling",
    current_temp: 75,
    setpoint: 72,
    cycle_id: "c1",
    cycle_started_at: "2024-01-01T12:00:00",
    rooms: [
      {
        room_id: "room1234",
        avg_temp: 74,
        vent_states: { "cover.living": "open", "cover.bed": "closed" },
        presence_active: true,
      },
      {
        room_id: "room5678",
        avg_temp: null,
        vent_states: {},
        presence_active: false,
      },
    ],
  },
];

describe("DevMode Page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getDevLogs).mockResolvedValue(mockDevLogs);
    vi.mocked(api.getStatus).mockResolvedValue([]);
    vi.mocked(api.getSystemStatus).mockResolvedValue({ enabled: true, dev_mode: true });
    vi.mocked(api.connectWS).mockReturnValue(() => {});
  });

  it("renders the dev mode page when devMode is enabled", async () => {
    render(
      <DevModeContext.Provider value={{ devMode: true, toggleDevMode: async () => {} }}>
        <DevMode />
      </DevModeContext.Provider>
    );
    expect(await screen.findByText(/🛠 Developer Mode/i)).toBeInTheDocument();
    expect(await screen.findByText(/Vent opened/i)).toBeInTheDocument();
  });

  it("renders restricted message when devMode is disabled", async () => {
    render(
      <DevModeContext.Provider value={{ devMode: false, toggleDevMode: async () => {} }}>
        <DevMode />
      </DevModeContext.Provider>
    );
    expect(screen.getByText(/Developer mode is off/i)).toBeInTheDocument();
  });

  it("allows clearing logs", async () => {
    render(
      <DevModeContext.Provider value={{ devMode: true, toggleDevMode: async () => {} }}>
        <DevMode />
      </DevModeContext.Provider>
    );

    expect(await screen.findByText(/Vent opened/i)).toBeInTheDocument();
    const clearBtn = screen.getByText("Clear");
    fireEvent.click(clearBtn);
    expect(screen.queryByText(/Vent opened/i)).not.toBeInTheDocument();
  });

  it("renders all action-feed icon variants and the temperature column", async () => {
    render(
      <DevModeContext.Provider value={{ devMode: true, toggleDevMode: async () => {} }}>
        <DevMode />
      </DevModeContext.Provider>
    );
    expect(await screen.findByText(/Vent opened/i)).toBeInTheDocument();
    expect(screen.getByText(/Vent closed/i)).toBeInTheDocument();
    expect(screen.getByText(/Setpoint set/i)).toBeInTheDocument();
    expect(screen.getByText(/Other action/i)).toBeInTheDocument();
    // set_thermostat entry carries a temperature rendered via fmtTemp (°F default)
    expect(screen.getByText(/72.0°F/)).toBeInTheDocument();
    // entity_id is shortened to its object id
    expect(screen.getByText("living_room")).toBeInTheDocument();
  });

  it("renders the zone panel with rooms, vents, and presence", async () => {
    vi.mocked(api.getStatus).mockResolvedValue(mockZones);
    render(
      <DevModeContext.Provider value={{ devMode: true, toggleDevMode: async () => {} }}>
        <DevMode />
      </DevModeContext.Provider>
    );
    expect(await screen.findByText("climate.main")).toBeInTheDocument();
    expect(screen.getByText("cooling")).toBeInTheDocument();
    // vent pills
    expect(screen.getByText("open")).toBeInTheDocument();
    expect(screen.getByText("closed")).toBeInTheDocument();
    // room with no avg temp / no vents renders a dash
    expect(screen.getByText("room5678")).toBeInTheDocument();
  });

  it("shows an empty zone message when there are no zones", async () => {
    vi.mocked(api.getStatus).mockResolvedValue([]);
    render(
      <DevModeContext.Provider value={{ devMode: true, toggleDevMode: async () => {} }}>
        <DevMode />
      </DevModeContext.Provider>
    );
    expect(await screen.findByText(/No thermostat zones found/i)).toBeInTheDocument();
  });

  it("shows an empty action-feed message when there are no entries", async () => {
    vi.mocked(api.getDevLogs).mockResolvedValue([]);
    render(
      <DevModeContext.Provider value={{ devMode: true, toggleDevMode: async () => {} }}>
        <DevMode />
      </DevModeContext.Provider>
    );
    expect(await screen.findByText(/No dev actions logged yet/i)).toBeInTheDocument();
  });

  it("calls toggleDevMode when enabling from the off state", async () => {
    const toggle = vi.fn(async () => {});
    render(
      <DevModeContext.Provider value={{ devMode: false, toggleDevMode: toggle }}>
        <DevMode />
      </DevModeContext.Provider>
    );
    fireEvent.click(screen.getByRole("button", { name: /Enable Developer Mode/i }));
    expect(toggle).toHaveBeenCalled();
  });

  it("calls toggleDevMode from the Disable button when active", async () => {
    const toggle = vi.fn(async () => {});
    render(
      <DevModeContext.Provider value={{ devMode: true, toggleDevMode: toggle }}>
        <DevMode />
      </DevModeContext.Provider>
    );
    await screen.findByText(/🛠 Developer Mode/i);
    fireEvent.click(screen.getByRole("button", { name: /Disable Dev Mode/i }));
    expect(toggle).toHaveBeenCalled();
  });

  it("ignores errors from the polling fetchers", async () => {
    vi.mocked(api.getDevLogs).mockRejectedValue(new Error("boom"));
    vi.mocked(api.getStatus).mockRejectedValue(new Error("boom"));
    render(
      <DevModeContext.Provider value={{ devMode: true, toggleDevMode: async () => {} }}>
        <DevMode />
      </DevModeContext.Provider>
    );
    // Still renders past the loading state despite both fetchers throwing
    await waitFor(() => {
      expect(screen.getByText(/🛠 Developer Mode/i)).toBeInTheDocument();
    });
  });
});
