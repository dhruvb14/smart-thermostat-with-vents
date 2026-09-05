import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import DevMode from "./DevMode";
import * as api from "../api";
import { DevModeContext } from "../contexts";

vi.mock("../api");

const renderDevMode = () =>
  render(
    <DevModeContext.Provider value={{ devMode: true, toggleDevMode: async () => {} }}>
      <DevMode />
    </DevModeContext.Provider>
  );

describe("DevMode — uncovered branches", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getDevLogs).mockResolvedValue([]);
    vi.mocked(api.getStatus).mockResolvedValue([]);
    vi.mocked(api.getSystemStatus).mockResolvedValue({ enabled: true, dev_mode: true });
    vi.mocked(api.connectWS).mockReturnValue(() => {});
  });

  // ── fmtEntity (lines 40-45) ───────────────────────────────────────────────

  it("shortens well-formed entity ids and copes with blank / dotless ones", async () => {
    vi.mocked(api.getDevLogs).mockResolvedValue([
      {
        id: 1,
        timestamp: "2024-01-01T12:00:00",
        message: "Normal entity",
        level: "info",
        category: "dev",
        details: { action: "open_vent", entity_id: "cover.living_room" },
      },
      {
        id: 2,
        timestamp: "2024-01-01T12:01:00",
        message: "Dotless entity",
        level: "info",
        category: "dev",
        // A malformed id with no domain separator must render whole, not
        // collapse to "undefined".
        details: { action: "open_vent", entity_id: "coverlivingroom" },
      },
      {
        id: 3,
        timestamp: "2024-01-01T12:02:00",
        message: "Blank entity",
        level: "info",
        category: "dev",
        // Present-but-empty: the cell renders (entity_id != null) but the
        // formatter has nothing to shorten.
        details: { action: "open_vent", entity_id: "" },
      },
    ]);

    renderDevMode();
    await screen.findByText("Normal entity");

    const cellFor = (msg: string) =>
      (screen.getByText(msg).closest(".dev-feed-row") as HTMLElement).querySelector(
        ".dev-feed-entity"
      ) as HTMLElement;

    expect(cellFor("Normal entity")).toHaveTextContent("living_room");
    expect(cellFor("Dotless entity")).toHaveTextContent("coverlivingroom");
    expect(cellFor("Blank entity").textContent).toBe("");
  });

  // ── ZonePanel fallbacks (lines 115-121) ───────────────────────────────────

  it("falls back to hvac_mode, then a dash, and dashes missing zone temps", async () => {
    vi.mocked(api.getStatus).mockResolvedValue([
      {
        thermostat_entity_id: "climate.a",
        cycle_state: "idle",
        hvac_mode: "heat",
        // No action reported → the badge falls back to the mode.
        hvac_action: "",
        current_temp: null,
        setpoint: null,
        cycle_id: null,
        cycle_started_at: null,
        rooms: [],
      },
      {
        thermostat_entity_id: "climate.b",
        cycle_state: "idle",
        // Neither action nor mode → the badge shows the em-dash placeholder.
        hvac_mode: "",
        hvac_action: "",
        current_temp: 70,
        setpoint: 68,
        cycle_id: null,
        cycle_started_at: null,
        rooms: [],
      },
    ]);

    renderDevMode();
    await screen.findByText("climate.a", { exact: false });

    const cardA = screen.getByText(/climate\.a/).closest(".card") as HTMLElement;
    expect(within(cardA).getByText("heat")).toBeInTheDocument();
    // Both temperature slots dash out when the thermostat reports nothing.
    expect(cardA).toHaveTextContent("Current: — · Setpoint: —");

    const cardB = screen.getByText(/climate\.b/).closest(".card") as HTMLElement;
    expect(within(cardB).getByText("—")).toBeInTheDocument();
    expect(cardB).toHaveTextContent("Current: 70.0°F · Setpoint: 68.0°F");
  });

  // ── Seed failure with a non-Error rejection (line 234) ────────────────────

  it("shows a generic message when seeding rejects with a non-Error value", async () => {
    // fetch()-layer failures can surface as a bare string; the UI must still
    // say something rather than rendering "[object Object]" or nothing.
    vi.mocked(api.seedDemoMetrics).mockRejectedValue("gateway went away");

    renderDevMode();
    fireEvent.click(await screen.findByText("Seed demo metrics"));

    expect(await screen.findByText("Seeding failed")).toBeInTheDocument();
  });
});
