import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import Settings from "./Settings";
import * as api from "../api";
import { UnitContext, buildUnitContext } from "../contexts";

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
    vacation_hvac_mode: "single" as const,
    min_cycle_runtime_min: 0,
    min_cycle_offtime_min: 0,
    cooling_lockout_below_f: null,
  },
];

describe("Settings Page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getSensorHealth).mockResolvedValue({ stale_after_min: 30, rooms: [] });
    vi.mocked(api.getSensorStaleness).mockResolvedValue({ stale_after_min: 30 });
    vi.mocked(api.getSensorHealth).mockResolvedValue({ stale_after_min: 30, rooms: [] });
    vi.mocked(api.getSensorStaleness).mockResolvedValue({ stale_after_min: 30 });
    vi.mocked(api.getThermostats).mockResolvedValue(mockThermostats);
    vi.mocked(api.getRooms).mockResolvedValue([]);
    vi.mocked(api.updateThermostat).mockResolvedValue(mockThermostats[0]);
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

describe("Settings Page — Celsius mode", () => {
  const renderInCelsius = () =>
    render(
      <UnitContext.Provider value={buildUnitContext("C")}>
        <Settings />
      </UnitContext.Provider>
    );

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getThermostats).mockResolvedValue(mockThermostats);
    vi.mocked(api.getRooms).mockResolvedValue([]);
    vi.mocked(api.updateThermostat).mockResolvedValue(mockThermostats[0]);
  });

  it("shows absolute and delta temp field labels with (°C)", async () => {
    renderInCelsius();
    await screen.findByText("climate.test");

    expect(screen.getByLabelText(/Min setpoint \(°C\)/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Max setpoint \(°C\)/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Deadband \(°C\)/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Overshoot delta \(°C\)/i)).toBeInTheDocument();
    // Non-temp field must not get (°C)
    expect(screen.queryByLabelText(/Min open vents \(°C\)/i)).toBeNull();
  });

  it("displays min_setpoint converted to °C", async () => {
    renderInCelsius();
    await screen.findByText("climate.test");

    // min_setpoint=60°F → toDisplay(60) = 15.6°C
    const minInput = document.getElementById("settings-min_setpoint") as HTMLInputElement;
    expect(parseFloat(minInput.value)).toBeCloseTo(15.6, 1);
  });

  it("validates min < max when values are entered in °C", async () => {
    renderInCelsius();
    await screen.findByText("climate.test");

    const minInput = document.getElementById("settings-min_setpoint") as HTMLInputElement;
    const maxInput = document.getElementById("settings-max_setpoint") as HTMLInputElement;

    // 30°C → toStorage(30) = 86°F; 25°C → toStorage(25) = 77°F → 86 >= 77 → error
    fireEvent.change(minInput, { target: { value: "30" } });
    fireEvent.change(maxInput, { target: { value: "25" } });
    fireEvent.click(screen.getByText("Save changes"));

    await waitFor(() => {
      expect(document.body.textContent).toContain("Min setpoint must be less than max setpoint");
    });
    expect(api.updateThermostat).not.toHaveBeenCalled();
  });

  it("converts °C input to °F when saving settings", async () => {
    renderInCelsius();
    await screen.findByText("climate.test");

    // Change min setpoint to 16°C → toStorage(16) = 60.8°F
    const minInput = document.getElementById("settings-min_setpoint") as HTMLInputElement;
    fireEvent.change(minInput, { target: { value: "16" } });

    fireEvent.click(screen.getByText("Save changes"));

    await waitFor(() => {
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({ min_setpoint: 60.8 })
      );
    });
  });
});

describe("Settings Page — Sensor-staleness threshold (Issue #211)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getSensorHealth).mockResolvedValue({ stale_after_min: 30, rooms: [] });
    vi.mocked(api.getSensorStaleness).mockResolvedValue({ stale_after_min: 30 });
    vi.mocked(api.getThermostats).mockResolvedValue(mockThermostats);
    vi.mocked(api.getRooms).mockResolvedValue([]);
  });

  it("renders the configured threshold and saves a new value", async () => {
    vi.mocked(api.setSensorStaleness).mockResolvedValue({ stale_after_min: 45 });
    render(<Settings />);

    const input = (await screen.findByLabelText(/^Minutes$/i)) as HTMLInputElement;
    // Defaults to the GET response.
    expect(input.value).toBe("30");

    fireEvent.change(input, { target: { value: "45" } });
    // The "Save" button next to the field — pick the first one (the SensorStalenessCard
    // sits above thermostat cards, each of which has its own "Save changes" button).
    fireEvent.click(screen.getAllByRole("button", { name: /^Save$/i })[0]);

    await waitFor(() => {
      expect(api.setSensorStaleness).toHaveBeenCalledWith(45);
    });
  });
});
