import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import Thermostats from "./Thermostats";
import * as api from "../api";
import { UnitContext, buildUnitContext } from "../contexts";

vi.mock("../api");

const mockThermostats: api.ThermostatConfig[] = [
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
  },
];

describe("Thermostats Page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getThermostats).mockResolvedValue(mockThermostats);
    vi.mocked(api.getHAEntities).mockResolvedValue([
      { entity_id: "climate.hallway", friendly_name: "Hallway", state: "" },
    ]);
    vi.mocked(api.downloadBackup).mockReturnValue(undefined);
  });

  it("renders thermostat list", async () => {
    render(<Thermostats />);
    expect(await screen.findByText("Main HVAC")).toBeInTheDocument();
    expect(screen.getByText("climate.test")).toBeInTheDocument();
  });

  it("validates friendly name in registration", async () => {
    render(<Thermostats />);
    fireEvent.click(await screen.findByText("+ Register thermostat"));

    const regBtn = await screen.findByRole("button", { name: "Register" });
    fireEvent.click(regBtn);
    expect(await screen.findByText("Select a thermostat entity")).toBeInTheDocument();
  });

  it("successfully registers a thermostat", async () => {
    vi.mocked(api.createThermostat).mockResolvedValue({
      thermostat_entity_id: "climate.hallway",
      name: "Hallway",
    } as api.ThermostatConfig);
    render(<Thermostats />);
    fireEvent.click(await screen.findByText("+ Register thermostat"));

    const nameInput = await screen.findByLabelText(/Friendly name/i, {
      selector: "#add-thermo-name",
    });
    fireEvent.change(nameInput, { target: { value: "Hallway HVAC" } });

    const pickerInput = screen.getByPlaceholderText(/Search thermostats/i);
    fireEvent.focus(pickerInput);
    fireEvent.change(pickerInput, { target: { value: "hallway" } });

    const option = await screen.findByText("Hallway");
    fireEvent.mouseDown(option);

    fireEvent.click(screen.getByRole("button", { name: "Register" }));

    await waitFor(() => {
      expect(api.createThermostat).toHaveBeenCalledWith({
        thermostat_entity_id: "climate.hallway",
        name: "Hallway HVAC",
      });
    });
  });

  it("updates thermostat settings", async () => {
    vi.mocked(api.updateThermostat).mockResolvedValue({} as api.ThermostatConfig);
    render(<Thermostats />);

    const nameInput = await screen.findByLabelText(/Friendly name/i, {
      selector: "#thermo-climate\\.test-name",
    });
    fireEvent.change(nameInput, { target: { value: "Updated Name" } });

    const card = nameInput.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByText("Save changes"));

    await waitFor(() => {
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({
          name: "Updated Name",
        })
      );
    });
  });

  it("shows validation error for min >= max setpoint", async () => {
    render(<Thermostats />);

    const minInput = await screen.findByLabelText(/Min setpoint/i);
    const maxInput = await screen.findByLabelText(/Max setpoint/i);

    fireEvent.change(minInput, { target: { value: "85" } });
    fireEvent.change(maxInput, { target: { value: "80" } });

    const card = minInput.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByText("Save changes"));

    expect(
      await screen.findByText("Min setpoint must be less than max setpoint")
    ).toBeInTheDocument();
  });

  it("handles backup download", async () => {
    render(<Thermostats />);
    const downloadBtn = await screen.findByText("Download backup");
    fireEvent.click(downloadBtn);
    expect(api.downloadBackup).toHaveBeenCalled();
  });

  it("handles restore from backup", async () => {
    window.confirm = vi.fn().mockReturnValue(true);
    vi.mocked(api.restoreBackup).mockResolvedValue(undefined);
    const { container } = render(<Thermostats />);

    // Wait for loading to finish
    await screen.findByText("Main HVAC");

    const fileInput = container.querySelector("#restore-backup-input") as HTMLInputElement;
    if (!fileInput) throw new Error("Could not find file input");

    const file = new File(["dummy content"], "test.db", { type: "application/x-sqlite3" });

    fireEvent.change(fileInput, { target: { files: [file] } });

    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => {
      expect(api.restoreBackup).toHaveBeenCalledWith(file);
    });
    expect(await screen.findByText(/Restore complete/i)).toBeInTheDocument();
  });
});

describe("Thermostats Page — vacation mode selector", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getThermostats).mockResolvedValue(mockThermostats);
    vi.mocked(api.getHAEntities).mockResolvedValue([]);
    vi.mocked(api.downloadBackup).mockReturnValue(undefined);
  });

  it("renders the Vacation HVAC mode selector", async () => {
    render(<Thermostats />);
    expect(await screen.findByLabelText(/Vacation HVAC mode/i)).toBeInTheDocument();
  });

  it("shows helper text for single setpoint mode", async () => {
    render(<Thermostats />);
    await screen.findByLabelText(/Vacation HVAC mode/i);
    expect(screen.getByText(/turns the HVAC.*off/i)).toBeInTheDocument();
  });

  it("shows helper text and Test button for range mode", async () => {
    vi.mocked(api.getThermostats).mockResolvedValue([
      { ...mockThermostats[0], vacation_hvac_mode: "range" as const },
    ]);
    render(<Thermostats />);
    await screen.findByLabelText(/Vacation HVAC mode/i);
    expect(screen.getAllByText(/Test auto mode/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/heat_cool.*auto/i)).toBeInTheDocument();
  });

  it("Test button calls testVacationMode", async () => {
    vi.mocked(api.getThermostats).mockResolvedValue([
      { ...mockThermostats[0], vacation_hvac_mode: "range" as const },
    ]);
    vi.mocked(api.testVacationMode).mockResolvedValue({
      ok: true,
      min_setpoint: 60,
      max_setpoint: 80,
      thermostat_state: {},
    });
    render(<Thermostats />);
    const testBtn = await screen.findByRole("button", { name: /Test auto mode/i });
    fireEvent.click(testBtn);
    await waitFor(() => expect(api.testVacationMode).toHaveBeenCalledWith("climate.test"));
    expect(await screen.findByText(/Check your thermostat/i)).toBeInTheDocument();
  });
});

describe("Thermostats Page — Celsius mode", () => {
  const renderInCelsius = () =>
    render(
      <UnitContext.Provider value={buildUnitContext("C")}>
        <Thermostats />
      </UnitContext.Provider>
    );

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getThermostats).mockResolvedValue(mockThermostats);
    vi.mocked(api.getHAEntities).mockResolvedValue([]);
    vi.mocked(api.downloadBackup).mockReturnValue(undefined);
  });

  it("shows absolute and delta temp field labels with (°C)", async () => {
    renderInCelsius();
    await screen.findByText("Main HVAC");

    // Absolute temp fields should have (°C) appended
    expect(screen.getByLabelText(/Min setpoint \(°C\)/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Max setpoint \(°C\)/i)).toBeInTheDocument();
    // Delta temp fields should also have (°C)
    expect(screen.getByLabelText(/Deadband \(°C\)/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Overshoot delta \(°C\)/i)).toBeInTheDocument();
    // Non-temp field should NOT have (°C)
    expect(screen.getByLabelText(/Min open vents/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Min open vents \(°C\)/i)).toBeNull();
  });

  it("displays min_setpoint converted to °C", async () => {
    renderInCelsius();
    await screen.findByText("Main HVAC");

    // min_setpoint=60°F → toDisplay(60) = (60-32)*5/9 = 15.6°C
    const minInput = screen.getByLabelText(/Min setpoint \(°C\)/i) as HTMLInputElement;
    expect(parseFloat(minInput.value)).toBeCloseTo(15.6, 1);
  });

  it("converts °C input to °F when saving thermostat settings", async () => {
    vi.mocked(api.updateThermostat).mockResolvedValue({} as api.ThermostatConfig);
    renderInCelsius();

    const nameInput = await screen.findByLabelText(/Friendly name/i, {
      selector: "#thermo-climate\\.test-name",
    });

    // Change min setpoint: 16°C → toStorage(16) = 16*9/5+32 = 60.8°F
    const minInput = screen.getByLabelText(/Min setpoint \(°C\)/i);
    fireEvent.change(minInput, { target: { value: "16" } });

    const card = nameInput.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByText("Save changes"));

    await waitFor(() => {
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({ min_setpoint: 60.8 })
      );
    });
  });

  it("converts deadband delta from °C to °F when saving", async () => {
    vi.mocked(api.updateThermostat).mockResolvedValue({} as api.ThermostatConfig);
    renderInCelsius();

    const nameInput = await screen.findByLabelText(/Friendly name/i, {
      selector: "#thermo-climate\\.test-name",
    });

    // deadband=0.5°F → displays as toDisplayDelta(0.5) = 0.5*5/9 ≈ 0.28°C
    // Change to 0.56°C → toStorageDelta(0.56) = 0.56*9/5 = 1.01°F (rounds to 2dp)
    const deadbandInput = screen.getByLabelText(/Deadband \(°C\)/i);
    fireEvent.change(deadbandInput, { target: { value: "1" } }); // 1°C delta → 1.8°F

    const card = nameInput.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByText("Save changes"));

    await waitFor(() => {
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({ deadband: 1.8 })
      );
    });
  });
});
