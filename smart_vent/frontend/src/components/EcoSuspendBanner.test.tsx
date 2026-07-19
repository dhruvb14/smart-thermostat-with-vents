import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import EcoSuspendBanner from "./EcoSuspendBanner";
import * as api from "../api";
import { ecoThermostatDefaults } from "../testFixtures";

vi.mock("../api");

function tc(over: Partial<api.ThermostatConfig> = {}): api.ThermostatConfig {
  return {
    thermostat_entity_id: "climate.up",
    name: "Upstairs",
    default_temp: 72,
    min_setpoint: 60,
    max_setpoint: 80,
    deadband: 0.5,
    max_vent_closed_min: 0,
    overshoot_delta: 2,
    cycle_timeout_hours: 2,
    reconciliation_interval_min: 0,
    vacation_hvac_mode: "single",
    min_cycle_runtime_min: 0,
    min_cycle_offtime_min: 0,
    cooling_lockout_below_f: null,
    total_vents_count: 4,
    has_bypass_damper: false,
    min_open_vents_fraction: 0.333,
    overflow_during_min_runtime: true,
    unavailable_abort_after_min: 5,
    ...ecoThermostatDefaults,
    ...over,
  };
}

describe("EcoSuspendBanner (Issue #500)", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders nothing when the thermostat fetch fails", async () => {
    vi.mocked(api.getThermostats).mockRejectedValue(new Error("network"));
    const { container } = render(<EcoSuspendBanner />);
    await waitFor(() => expect(api.getThermostats).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("falls back to the raw string for an unparseable resume_at", async () => {
    vi.mocked(api.getThermostats).mockResolvedValue([tc({ eco_suspend_until: "not-a-date" })]);
    render(<EcoSuspendBanner />);
    expect(await screen.findByText(/not-a-date/)).toBeInTheDocument();
  });

  it("renders nothing when no thermostat is suspended", async () => {
    vi.mocked(api.getThermostats).mockResolvedValue([tc()]);
    const { container } = render(<EcoSuspendBanner />);
    await waitFor(() => expect(api.getThermostats).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("renders one banner listing every suspended thermostat", async () => {
    vi.mocked(api.getThermostats).mockResolvedValue([
      tc({ eco_suspend_until: "2099-12-25T10:00:00+00:00" }),
      tc({
        thermostat_entity_id: "climate.down",
        name: "Downstairs",
        eco_suspend_until: "2099-12-26T10:00:00+00:00",
      }),
      tc({ thermostat_entity_id: "climate.attic", name: "Attic" }),
    ]);
    render(<EcoSuspendBanner />);
    expect(await screen.findByText(/Eco Mode suspended/i)).toBeInTheDocument();
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.getByText("Upstairs")).toBeInTheDocument();
    expect(screen.getByText("Downstairs")).toBeInTheDocument();
    expect(screen.queryByText("Attic")).not.toBeInTheDocument();
    // Each thermostat renders its OWN resume date — the suspensions are
    // fully independent (#500 per-thermostat contract).
    const banner = screen.getByTestId("eco-suspend-banner");
    expect(banner).toHaveTextContent(new Date("2099-12-25T10:00:00+00:00").toLocaleString());
    expect(banner).toHaveTextContent(new Date("2099-12-26T10:00:00+00:00").toLocaleString());
  });

  it("opens the manage modal when the banner card is clicked", async () => {
    vi.mocked(api.getThermostats).mockResolvedValue([
      tc({ eco_suspend_until: "2099-12-25T10:00:00+00:00" }),
    ]);
    render(<EcoSuspendBanner />);
    fireEvent.click(await screen.findByTestId("eco-suspend-banner"));
    expect(screen.getByRole("button", { name: /Resume Eco now/i })).toBeInTheDocument();
    // Pre-scoped to the (first) suspended thermostat.
    expect(screen.getByLabelText(/Thermostat/i)).toHaveValue("climate.up");
  });

  it("opens the modal via the Manage button and refreshes after a change", async () => {
    vi.mocked(api.getThermostats).mockResolvedValue([
      tc({ eco_suspend_until: "2099-12-25T10:00:00+00:00" }),
    ]);
    vi.mocked(api.clearEcoSuspend).mockResolvedValue({
      thermostat_entity_id: "climate.up",
      resume_at: null,
    });
    render(<EcoSuspendBanner />);
    fireEvent.click(await screen.findByText("Manage"));
    fireEvent.click(screen.getByRole("button", { name: /Resume Eco now/i }));
    await waitFor(() => expect(api.clearEcoSuspend).toHaveBeenCalledWith("climate.up"));
    // onChanged refetches the suspension state.
    await waitFor(() => expect(api.getThermostats).toHaveBeenCalledTimes(2));
  });
});
