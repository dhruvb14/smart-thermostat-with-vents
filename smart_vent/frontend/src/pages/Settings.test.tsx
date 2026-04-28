import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import Settings from "./Settings";
import * as api from "../api";

vi.mock("../api");

const mockThermostats: api.ThermostatConfig[] = [
  {
    thermostat_entity_id: "climate.test",
    name: "Test Thermostat",
    default_temp: 72,
    min_setpoint: 60,
    max_setpoint: 80,
    deadband: 0.5,
    max_vent_closed_min: 60,
    min_open_vents: 1,
    overshoot_delta: 0.5,
    cycle_timeout_hours: 2,
    reconciliation_interval_min: 5,
  },
];

describe("Settings Page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.getThermostats as any).mockResolvedValue(mockThermostats);
    (api.getRooms as any).mockResolvedValue([]);
    (api.updateThermostat as any).mockResolvedValue(mockThermostats[0]);
  });

  it("renders thermostat settings", async () => {
    render(<Settings />);
    expect(await screen.findByText("climate.test")).toBeInTheDocument();
  });

  it("validates min < max setpoint", async () => {
    render(<Settings />);
    await screen.findByText("climate.test");

    // Select inputs by ID
    const minInput = document.getElementById("settings-min_setpoint") as HTMLInputElement;
    const maxInput = document.getElementById("settings-max_setpoint") as HTMLInputElement;

    fireEvent.change(minInput, { target: { value: "85" } });
    fireEvent.change(maxInput, { target: { value: "80" } });

    // Try to save
    fireEvent.click(screen.getByText("Save changes"));

    // Check for error text directly in the body
    await waitFor(() => {
      const bodyText = document.body.textContent;
      expect(bodyText).toContain("Min setpoint must be less than max setpoint");
    });

    expect(api.updateThermostat).not.toHaveBeenCalled();
  });
});
