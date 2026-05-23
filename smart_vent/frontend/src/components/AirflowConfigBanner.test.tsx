import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import AirflowConfigBanner from "./AirflowConfigBanner";
import * as api from "../api";

vi.mock("../api");

function tc(over: Partial<api.ThermostatConfig> = {}): api.ThermostatConfig {
  return {
    thermostat_entity_id: "climate.test",
    name: "Test HVAC",
    default_temp: 72,
    min_setpoint: 60,
    max_setpoint: 80,
    deadband: 0.5,
    max_vent_closed_min: 0,
    overshoot_delta: 2,
    cycle_timeout_hours: 3,
    reconciliation_interval_min: 0,
    vacation_hvac_mode: "single",
    min_cycle_runtime_min: 0,
    min_cycle_offtime_min: 0,
    cooling_lockout_below_f: null,
    total_vents_count: null,
    has_bypass_damper: false,
    min_open_vents_fraction: 0.333,
    ...over,
  };
}

describe("AirflowConfigBanner (Issue #213)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders a singular warning when one thermostat needs configuration", async () => {
    vi.mocked(api.getThermostats).mockResolvedValue([
      tc({ thermostat_entity_id: "climate.one", name: "Upstairs" }),
    ]);
    render(<AirflowConfigBanner />);
    const banner = await screen.findByTestId("airflow-config-banner");
    expect(banner).toHaveTextContent(/Action required/);
    expect(banner).toHaveTextContent(/Upstairs/);
    expect(banner).toHaveTextContent(/transitional default/);
  });

  it("lists multiple thermostats when several need configuration", async () => {
    vi.mocked(api.getThermostats).mockResolvedValue([
      tc({ thermostat_entity_id: "climate.one", name: "Upstairs" }),
      tc({ thermostat_entity_id: "climate.two", name: "Downstairs" }),
    ]);
    render(<AirflowConfigBanner />);
    const banner = await screen.findByTestId("airflow-config-banner");
    expect(banner).toHaveTextContent(/2 thermostats/);
    expect(banner).toHaveTextContent(/Upstairs/);
    expect(banner).toHaveTextContent(/Downstairs/);
  });

  it("does not render when every thermostat is properly configured", async () => {
    vi.mocked(api.getThermostats).mockResolvedValue([
      tc({ total_vents_count: 12 }), // configured by setting total_vents_count
      tc({ thermostat_entity_id: "climate.two", has_bypass_damper: true }), // configured via bypass damper
    ]);
    render(<AirflowConfigBanner />);
    // Wait long enough for the effect to settle.
    await waitFor(() => expect(api.getThermostats).toHaveBeenCalled());
    expect(screen.queryByTestId("airflow-config-banner")).not.toBeInTheDocument();
  });

  it("does not render when getThermostats fails — failing softly avoids nagging on a network blip", async () => {
    vi.mocked(api.getThermostats).mockRejectedValue(new Error("network"));
    render(<AirflowConfigBanner />);
    await waitFor(() => expect(api.getThermostats).toHaveBeenCalled());
    expect(screen.queryByTestId("airflow-config-banner")).not.toBeInTheDocument();
  });
});
