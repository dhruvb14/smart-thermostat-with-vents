import { describe, it, expect, vi, afterEach } from "vitest";
import { ecoRoomDefaults } from "../testFixtures";
import { render, screen } from "@testing-library/react";

// The live active-rooms list is frozen out of the golden under CI (engine-driven,
// non-deterministic). To keep the requesting-temp line and the Clear-presence
// button under visual-regression coverage, Dashboard renders a fixed sample row
// in the frozen slot on the first zone card. This exercises that CI-only branch:
// the module-level isCI flag is derived from VITE_APP_VERSION, so we stub it and
// re-import with a fresh module registry (same approach as ci.test.tsx).

vi.mock("../api");

const mockStatus = [
  {
    thermostat_entity_id: "climate.test",
    hvac_mode: "cool",
    hvac_action: "cooling",
    current_temp: 75.2,
    setpoint: 72.0,
    cycle_state: "running" as const,
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

describe("Dashboard — CI build", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("renders the deterministic active-rooms sample (with Clear presence) in place of the frozen live list", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_APP_VERSION", "CI");

    const api = await import("../api");
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
        ...ecoRoomDefaults,
      },
    ]);
    vi.mocked(api.getThermostats).mockResolvedValue([]);

    const { default: Dashboard } = await import("./Dashboard");

    render(<Dashboard />);

    // The frozen sample renders two rooms, each with its own requesting-temp
    // line and Clear-presence button — proving both surfaces are per-room.
    expect(await screen.findByText("Bedroom")).toBeInTheDocument();
    expect(screen.getByText("Office")).toBeInTheDocument();
    expect(screen.getByText(/requesting 68.0°F/)).toBeInTheDocument();
    expect(screen.getByText(/requesting 70.0°F/)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /Clear presence/i })).toHaveLength(2);
    // The real, engine-driven active room is frozen out under CI.
    expect(screen.queryByText("Living Room")).not.toBeInTheDocument();
  });
});
