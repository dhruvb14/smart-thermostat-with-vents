import { describe, it, expect, vi, beforeEach } from "vitest";
import { ecoThermostatDefaults } from "../testFixtures";
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
    total_vents_count: null,

    has_bypass_damper: false,

    min_open_vents_fraction: 0.333,
    overshoot_delta: 0.5,
    cycle_timeout_hours: 2,
    reconciliation_interval_min: 5,
    vacation_hvac_mode: "single" as const,
    min_cycle_runtime_min: 0,
    min_cycle_offtime_min: 0,
    cooling_lockout_below_f: null,
    overflow_during_min_runtime: true,
    unavailable_abort_after_min: 5,
    ...ecoThermostatDefaults,
  },
];

describe("Thermostats Page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: null,
      current_value: null,
    });
    vi.mocked(api.getSensorStaleness).mockResolvedValue({ stale_after_min: 30 });
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

    // Airflow-floor (#213): total vent count is required at registration.
    // Scope to the modal so we don't pick up the per-card field on the page below.
    const ventsInput = document.getElementById("add-thermo-total-vents") as HTMLInputElement;
    fireEvent.change(ventsInput, { target: { value: "8" } });

    fireEvent.click(screen.getByRole("button", { name: "Register" }));

    await waitFor(() => {
      expect(api.createThermostat).toHaveBeenCalledWith({
        thermostat_entity_id: "climate.hallway",
        name: "Hallway HVAC",
        total_vents_count: 8,
        has_bypass_damper: false,
      });
    });
  });

  it("disables the airflow fraction slider when the bypass-damper checkbox is ticked (Issue #213)", async () => {
    vi.mocked(api.updateThermostat).mockResolvedValue({} as api.ThermostatConfig);
    render(<Thermostats />);

    const totalVentsInput = await screen.findByLabelText(/^Total vent count$/);
    expect(totalVentsInput).toBeInTheDocument();

    const fractionSlider = document.getElementById(
      "thermo-climate.test-fraction"
    ) as HTMLInputElement;
    const bypassCheckbox = document.getElementById(
      "thermo-climate.test-bypass-damper"
    ) as HTMLInputElement;

    expect(fractionSlider.disabled).toBe(false);

    fireEvent.click(bypassCheckbox);
    expect(fractionSlider.disabled).toBe(true);

    // The hint text changes to explain why the slider isn't enforced.
    expect(screen.getByText(/bypass damper handles pressure relief/i)).toBeInTheDocument();
  });

  it("saves the airflow-floor fields together (Issue #213)", async () => {
    vi.mocked(api.updateThermostat).mockResolvedValue({} as api.ThermostatConfig);
    render(<Thermostats />);

    const totalVentsInput = await screen.findByLabelText(/^Total vent count$/);
    fireEvent.change(totalVentsInput, { target: { value: "12" } });

    const fractionSlider = document.getElementById(
      "thermo-climate.test-fraction"
    ) as HTMLInputElement;
    fireEvent.change(fractionSlider, { target: { value: "0.5" } });

    const card = totalVentsInput.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByText("Save changes"));

    await waitFor(() => {
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({
          total_vents_count: 12,
          min_open_vents_fraction: 0.5,
        })
      );
    });
  });

  it("keeps saved edits after a unit-context identity change instead of regressing to pre-save values (Issue #293)", async () => {
    vi.mocked(api.updateThermostat).mockResolvedValue({
      ...mockThermostats[0],
      name: "Renamed HVAC",
    });

    const { rerender } = render(
      <UnitContext.Provider value={buildUnitContext("F")}>
        <Thermostats />
      </UnitContext.Provider>
    );

    const nameInput = (await screen.findByDisplayValue("Main HVAC")) as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: "Renamed HVAC" } });

    const card = nameInput.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByText("Save changes"));

    await waitFor(() =>
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({ name: "Renamed HVAC" })
      )
    );
    // Wait until the save fully settles (onSaved → parent baseline updated).
    await screen.findByText("Saved!");

    // Simulate App re-rendering with fresh unit-context identities (the bug's
    // trigger — e.g. toggling System/Dev). The card's reset effect fires; it
    // must restore the just-SAVED name, not the pre-save page-load value.
    rerender(
      <UnitContext.Provider value={buildUnitContext("F")}>
        <Thermostats />
      </UnitContext.Provider>
    );

    expect(screen.getByDisplayValue("Renamed HVAC")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("Main HVAC")).not.toBeInTheDocument();
  });

  it("blocks registration when total vent count is missing (Issue #213)", async () => {
    render(<Thermostats />);
    fireEvent.click(await screen.findByText("+ Register thermostat"));

    const nameInput = await screen.findByLabelText(/Friendly name/i, {
      selector: "#add-thermo-name",
    });
    fireEvent.change(nameInput, { target: { value: "Hallway HVAC" } });
    const pickerInput = screen.getByPlaceholderText(/Search thermostats/i);
    fireEvent.focus(pickerInput);
    fireEvent.change(pickerInput, { target: { value: "hallway" } });
    fireEvent.mouseDown(await screen.findByText("Hallway"));

    // No total_vents_count filled in → submission rejected with a message that
    // tells the user exactly what to do.
    fireEvent.click(screen.getByRole("button", { name: "Register" }));
    expect(await screen.findByText(/Total vent count is required/i)).toBeInTheDocument();
    expect(api.createThermostat).not.toHaveBeenCalled();
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

  it("edits and saves the short-cycle protection settings", async () => {
    vi.mocked(api.updateThermostat).mockResolvedValue({} as api.ThermostatConfig);
    render(<Thermostats />);

    const runtimeInput = await screen.findByLabelText(/Min cycle runtime/i);
    const offtimeInput = await screen.findByLabelText(/Min compressor off-time/i);
    fireEvent.change(runtimeInput, { target: { value: "10" } });
    fireEvent.change(offtimeInput, { target: { value: "5" } });

    const card = runtimeInput.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByText("Save changes"));

    await waitFor(() => {
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({
          min_cycle_runtime_min: 10,
          min_cycle_offtime_min: 5,
        })
      );
    });
  });

  it("edits and saves the thermostat-unavailability abort threshold (Issue #267)", async () => {
    vi.mocked(api.updateThermostat).mockResolvedValue({} as api.ThermostatConfig);
    render(<Thermostats />);

    const input = (await screen.findByLabelText(
      /Abort when thermostat unavailable/i
    )) as HTMLInputElement;
    // Initializes from the config value, no unit conversion (it's minutes).
    expect(input.value).toBe("5");
    // The helper text explains the consequence, including the 0 = never case.
    expect(screen.getByText(/cannot supervise the cycle/i)).toBeInTheDocument();
    expect(screen.getByText(/0 = never abort/i)).toBeInTheDocument();

    fireEvent.change(input, { target: { value: "12" } });
    const card = input.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByText("Save changes"));

    await waitFor(() => {
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({ unavailable_abort_after_min: 12 })
      );
    });
  });

  it("disables the overflow-conditioning checkbox when Min cycle runtime is 0 (Issue #237)", async () => {
    // Default mock has min_cycle_runtime_min = 0 — the toggle has nothing
    // to hook into (no hold ever happens), so it is disabled with a hint.
    render(<Thermostats />);
    const overflowToggle = (await screen.findByLabelText(
      /Redirect surplus air to other rooms/i
    )) as HTMLInputElement;
    expect(overflowToggle).toBeDisabled();
  });

  it("enables and saves the overflow-conditioning toggle (Issue #237)", async () => {
    vi.mocked(api.updateThermostat).mockResolvedValue({} as api.ThermostatConfig);
    render(<Thermostats />);

    // First, give the min-runtime hold a non-zero value so the toggle is enabled.
    const runtimeInput = await screen.findByLabelText(/Min cycle runtime/i);
    fireEvent.change(runtimeInput, { target: { value: "10" } });

    const overflowToggle = (await screen.findByLabelText(
      /Redirect surplus air to other rooms/i
    )) as HTMLInputElement;
    expect(overflowToggle).not.toBeDisabled();
    expect(overflowToggle.checked).toBe(true); // default true from mock
    fireEvent.click(overflowToggle);
    expect(overflowToggle.checked).toBe(false);

    const card = runtimeInput.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByText("Save changes"));

    await waitFor(() => {
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({
          overflow_during_min_runtime: false,
          unavailable_abort_after_min: 5,
        })
      );
    });
  });

  it("edits and saves the cooling lockout temperature", async () => {
    vi.mocked(api.updateThermostat).mockResolvedValue({} as api.ThermostatConfig);
    render(<Thermostats />);

    const lockoutInput = await screen.findByLabelText(/Cooling lockout/i);
    fireEvent.change(lockoutInput, { target: { value: "55" } });

    const card = lockoutInput.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByText("Save changes"));

    await waitFor(() => {
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({ cooling_lockout_below_f: 55 })
      );
    });
  });

  it("clears the cooling lockout when the field is emptied", async () => {
    vi.mocked(api.updateThermostat).mockResolvedValue({} as api.ThermostatConfig);
    render(<Thermostats />);

    const lockoutInput = await screen.findByLabelText(/Cooling lockout/i);
    fireEvent.change(lockoutInput, { target: { value: "55" } });
    fireEvent.change(lockoutInput, { target: { value: "" } });

    const card = lockoutInput.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByText("Save changes"));

    await waitFor(() => {
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({ cooling_lockout_below_f: null })
      );
    });
  });

  it("renders the house-wide outside temperature sensor picker", async () => {
    render(<Thermostats />);
    expect(await screen.findByText("Outside temperature sensor")).toBeInTheDocument();
  });

  it("saves the outside temperature sensor selection", async () => {
    vi.mocked(api.getHAEntities).mockResolvedValue([
      { entity_id: "sensor.outdoor", friendly_name: "Outdoor", state: "50" },
    ]);
    vi.mocked(api.setOutsideTempEntity).mockResolvedValue({
      entity_id: "sensor.outdoor",
      current_value: 50,
    });
    render(<Thermostats />);

    const searchInput = await screen.findByPlaceholderText(/Search sensor \/ weather entities/i);
    fireEvent.focus(searchInput);
    fireEvent.change(searchInput, { target: { value: "outdoor" } });

    const option = await screen.findByText("Outdoor");
    fireEvent.mouseDown(option);

    await waitFor(() => {
      expect(api.setOutsideTempEntity).toHaveBeenCalledWith("sensor.outdoor");
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
});

describe("Thermostats Page — vacation mode selector", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: null,
      current_value: null,
    });
    vi.mocked(api.getSensorStaleness).mockResolvedValue({ stale_after_min: 30 });
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

  it("Test button calls testVacationMode and shows Revert button", async () => {
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
    expect(screen.getByRole("button", { name: /Revert test/i })).toBeInTheDocument();
  });

  it("Revert button calls revertVacationTest", async () => {
    vi.mocked(api.getThermostats).mockResolvedValue([
      { ...mockThermostats[0], vacation_hvac_mode: "range" as const },
    ]);
    vi.mocked(api.testVacationMode).mockResolvedValue({
      ok: true,
      min_setpoint: 60,
      max_setpoint: 80,
      thermostat_state: {},
    });
    vi.mocked(api.revertVacationTest).mockResolvedValue({ ok: true });
    render(<Thermostats />);
    const testBtn = await screen.findByRole("button", { name: /Test auto mode/i });
    fireEvent.click(testBtn);
    const revertBtn = await screen.findByRole("button", { name: /Revert test/i });
    fireEvent.click(revertBtn);
    await waitFor(() => expect(api.revertVacationTest).toHaveBeenCalledWith("climate.test"));
    expect(await screen.findByText(/Reverted/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Test auto mode/i })).toBeInTheDocument();
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
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: null,
      current_value: null,
    });
    vi.mocked(api.getSensorStaleness).mockResolvedValue({ stale_after_min: 30 });
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
    // Non-temp field should NOT have (°C). Total vent count is one of the
    // new airflow-floor fields (#213) — a unit-less integer.
    expect(screen.getByLabelText(/Total vent count$/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Total vent count \(°C\)/i)).toBeNull();
  });

  it("displays min_setpoint converted to °C", async () => {
    renderInCelsius();
    await screen.findByText("Main HVAC");

    // min_setpoint=60°F → toDisplay(60) = (60-32)*5/9 = 15.6°C
    const minInput = screen.getByLabelText(/Min setpoint \(°C\)/i) as HTMLInputElement;
    expect(parseFloat(minInput.value)).toBeCloseTo(15.6, 1);
  });

  it("sends the user's raw °C value when saving thermostat settings (#231)", async () => {
    vi.mocked(api.updateThermostat).mockResolvedValue({} as api.ThermostatConfig);
    renderInCelsius();

    const nameInput = await screen.findByLabelText(/Friendly name/i, {
      selector: "#thermo-climate\\.test-name",
    });

    // The frontend MUST send the display-unit value as-is — the backend's
    // _to_f converts °C → °F on the write boundary. Sending pre-converted
    // °F here would cause double conversion (#231).
    const minInput = screen.getByLabelText(/Min setpoint \(°C\)/i);
    fireEvent.change(minInput, { target: { value: "16" } });

    const card = nameInput.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByText("Save changes"));

    await waitFor(() => {
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({ min_setpoint: 16 })
      );
    });
  });

  it("sends deadband as raw °C delta when saving (#231)", async () => {
    vi.mocked(api.updateThermostat).mockResolvedValue({} as api.ThermostatConfig);
    renderInCelsius();

    const nameInput = await screen.findByLabelText(/Friendly name/i, {
      selector: "#thermo-climate\\.test-name",
    });

    // Backend _delta_to_f handles °C delta → °F delta. Frontend stays out
    // of conversion to avoid the double-conversion bug (#231).
    const deadbandInput = screen.getByLabelText(/Deadband \(°C\)/i);
    fireEvent.change(deadbandInput, { target: { value: "1" } });

    const card = nameInput.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByText("Save changes"));

    await waitFor(() => {
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({ deadband: 1 })
      );
    });
  });

  it("never POSTs pre-converted °F when in Celsius mode (#231)", async () => {
    // Regression test for the double-conversion bug. The frontend MUST NOT
    // call toStorage/toStorageDelta on outgoing payloads — sending °F here
    // while the backend's _to_f also runs would compound the conversion
    // (e.g. 16°C → 60.8 → 141.44°F stored).
    vi.mocked(api.updateThermostat).mockResolvedValue({} as api.ThermostatConfig);
    renderInCelsius();

    const nameInput = await screen.findByLabelText(/Friendly name/i, {
      selector: "#thermo-climate\\.test-name",
    });

    fireEvent.change(screen.getByLabelText(/Min setpoint \(°C\)/i), { target: { value: "16" } });
    fireEvent.change(screen.getByLabelText(/Max setpoint \(°C\)/i), { target: { value: "27" } });
    fireEvent.change(screen.getByLabelText(/Deadband \(°C\)/i), { target: { value: "0.3" } });
    fireEvent.change(screen.getByLabelText(/Overshoot delta \(°C\)/i), {
      target: { value: "0.3" },
    });

    const card = nameInput.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByText("Save changes"));

    await waitFor(() => {
      expect(api.updateThermostat).toHaveBeenCalled();
    });
    const [, payload] = vi.mocked(api.updateThermostat).mock.calls[0];
    expect(payload.min_setpoint).toBe(16);
    expect(payload.max_setpoint).toBe(27);
    expect(payload.deadband).toBe(0.3);
    expect(payload.overshoot_delta).toBe(0.3);
    // Specifically guard against the buggy pre-converted °F values.
    expect(payload.min_setpoint).not.toBe(60.8);
    expect(payload.max_setpoint).not.toBe(80.6);
    expect(payload.deadband).not.toBe(0.54);
  });

  it("sends the user's raw °C eco override fields when saving thermostat settings (#231, #417)", async () => {
    // eco_cooling_outdoor_threshold is absolute (a pre-converted payload would
    // wrongly carry 86); eco_heating_max_drift is a delta (would wrongly carry
    // 3.6). Both must arrive as the raw °C number the user typed — the eco
    // fields render regardless of the Eco Mode toggle, so no outside sensor is
    // needed to exercise this write path.
    vi.mocked(api.updateThermostat).mockResolvedValue({} as api.ThermostatConfig);
    renderInCelsius();

    const nameInput = await screen.findByLabelText(/Friendly name/i, {
      selector: "#thermo-climate\\.test-name",
    });

    fireEvent.change(screen.getByLabelText(/Cooling.*outdoor threshold/i), {
      target: { value: "30" },
    });
    fireEvent.change(screen.getByLabelText(/Heating.*max drift/i), {
      target: { value: "2" },
    });

    const card = nameInput.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByText("Save changes"));

    await waitFor(() => {
      expect(api.updateThermostat).toHaveBeenCalled();
    });
    const [, payload] = vi.mocked(api.updateThermostat).mock.calls[0];
    expect(payload.eco_cooling_outdoor_threshold).toBe(30);
    expect(payload.eco_cooling_outdoor_threshold).not.toBe(86);
    expect(payload.eco_heating_max_drift).toBe(2);
    expect(payload.eco_heating_max_drift).not.toBe(3.6);
  });
});

describe("Thermostats Page — Sensor-staleness threshold (Issue #211)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: null,
      current_value: null,
    });
    vi.mocked(api.getThermostats).mockResolvedValue(mockThermostats);
    vi.mocked(api.getHAEntities).mockResolvedValue([]);
    vi.mocked(api.downloadBackup).mockReturnValue(undefined);
    vi.mocked(api.getSensorStaleness).mockResolvedValue({ stale_after_min: 30 });
  });

  it("renders the configured threshold and saves a new value", async () => {
    vi.mocked(api.setSensorStaleness).mockResolvedValue({ stale_after_min: 45 });
    render(<Thermostats />);

    const input = (await screen.findByLabelText(/^Minutes$/i)) as HTMLInputElement;
    expect(input.value).toBe("30");

    fireEvent.change(input, { target: { value: "45" } });
    // Scope the click to the staleness card so we don't pick up "Save changes"
    // buttons on the per-thermostat cards.
    const card = input.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByRole("button", { name: /^Save$/i }));

    await waitFor(() => {
      expect(api.setSensorStaleness).toHaveBeenCalledWith(45);
    });
  });

  it("falls back to 30 minutes when the GET fails", async () => {
    vi.mocked(api.getSensorStaleness).mockRejectedValue(new Error("network down"));
    render(<Thermostats />);

    const input = (await screen.findByLabelText(/^Minutes$/i)) as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe("30"));
  });

  it("surfaces the error message when the PUT fails", async () => {
    vi.mocked(api.setSensorStaleness).mockRejectedValue(new Error("nope"));
    render(<Thermostats />);

    const input = (await screen.findByLabelText(/^Minutes$/i)) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "42" } });
    const card = input.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByRole("button", { name: /^Save$/i }));

    await screen.findByText("nope");
  });
});

describe("Thermostats Page — empty state", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: null,
      current_value: null,
    });
    vi.mocked(api.getSensorStaleness).mockResolvedValue({ stale_after_min: 30 });
    vi.mocked(api.getThermostats).mockResolvedValue([]);
    vi.mocked(api.getHAEntities).mockResolvedValue([]);
    vi.mocked(api.downloadBackup).mockReturnValue(undefined);
  });

  it("shows an empty-state card when no thermostats are registered", async () => {
    render(<Thermostats />);
    expect(await screen.findByText(/No thermostats registered yet/i)).toBeInTheDocument();
  });
});

describe("Thermostats Page — Eco Mode (#404)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getSensorStaleness).mockResolvedValue({ stale_after_min: 30 });
    vi.mocked(api.getThermostats).mockResolvedValue(mockThermostats);
    vi.mocked(api.getHAEntities).mockResolvedValue([]);
    vi.mocked(api.downloadBackup).mockReturnValue(undefined);
  });

  it("enables the Eco toggle and saves eco settings once an outside sensor is configured", async () => {
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: "sensor.out",
      current_value: 80,
    });
    vi.mocked(api.updateThermostat).mockResolvedValue({} as api.ThermostatConfig);
    render(<Thermostats />);

    const ecoToggle = (await screen.findByLabelText(
      /Enable Eco Mode for this thermostat/i
    )) as HTMLInputElement;
    await waitFor(() => expect(ecoToggle).not.toBeDisabled());
    fireEvent.click(ecoToggle);
    expect(ecoToggle.checked).toBe(true);

    // Edit an eco numeric field — exercises the onChange handler.
    const threshInput = screen.getByLabelText(/Cooling.*outdoor threshold/i) as HTMLInputElement;
    fireEvent.change(threshInput, { target: { value: "92" } });
    // Blanking a numeric eco field coerces to 0 (the `|| 0` path).
    const driftInput = screen.getByLabelText(/Cooling.*max drift/i) as HTMLInputElement;
    fireEvent.change(driftInput, { target: { value: "" } });

    const card = ecoToggle.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByText("Save changes"));

    await waitFor(() => {
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({
          eco_mode_enabled: true,
          eco_cooling_outdoor_threshold: 92,
          eco_cooling_max_drift: 0,
        })
      );
    });
  });

  it("hides the PirateWeather gating hint when an outside sensor exists", async () => {
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: "sensor.out",
      current_value: 80,
    });
    render(<Thermostats />);
    await screen.findByText("Main HVAC");
    await waitFor(() => {
      expect(screen.queryByText(/Add a free weather integration such as/i)).not.toBeInTheDocument();
    });
  });

  it("disables the Eco toggle and shows the PirateWeather hint without a sensor", async () => {
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: null,
      current_value: null,
    });
    render(<Thermostats />);

    const ecoToggle = (await screen.findByLabelText(
      /Enable Eco Mode for this thermostat/i
    )) as HTMLInputElement;
    await waitFor(() => expect(ecoToggle).toBeDisabled());
    expect(screen.getByText(/Add a free weather integration such as/i)).toBeInTheDocument();
    expect(screen.getAllByText(/PirateWeather/i).length).toBeGreaterThan(0);
  });

  it("blocks saving when Eco is on but no outside sensor is configured", async () => {
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: null,
      current_value: null,
    });
    // Config already has Eco on, so the toggle stays enabled even without a
    // sensor — the guard lives in the save handler.
    vi.mocked(api.getThermostats).mockResolvedValue([
      { ...mockThermostats[0], eco_mode_enabled: true },
    ]);
    vi.mocked(api.updateThermostat).mockResolvedValue({} as api.ThermostatConfig);
    render(<Thermostats />);

    const ecoToggle = (await screen.findByLabelText(
      /Enable Eco Mode for this thermostat/i
    )) as HTMLInputElement;
    expect(ecoToggle.checked).toBe(true);

    const card = ecoToggle.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByText("Save changes"));

    expect(await screen.findByText(/before enabling Eco Mode/i)).toBeInTheDocument();
    expect(api.updateThermostat).not.toHaveBeenCalled();
  });

  it("renders unchecked with blank eco inputs when the config carries no eco values", async () => {
    // Defensive path (e.g. an older DB row predating Eco Mode): the boolean and
    // numeric eco fields arrive null, so the checkbox falls back to unchecked
    // and each numeric input renders blank rather than "null".
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: null,
      current_value: null,
    });
    vi.mocked(api.getThermostats).mockResolvedValue([
      {
        ...mockThermostats[0],
        eco_mode_enabled: null,
        eco_cooling_outdoor_threshold: null,
        eco_cooling_full_drift_temp: null,
        eco_cooling_max_drift: null,
        eco_heating_outdoor_threshold: null,
        eco_heating_full_drift_temp: null,
        eco_heating_max_drift: null,
        eco_hysteresis_band: null,
      } as unknown as api.ThermostatConfig,
    ]);
    render(<Thermostats />);

    const ecoToggle = (await screen.findByLabelText(
      /Enable Eco Mode for this thermostat/i
    )) as HTMLInputElement;
    expect(ecoToggle.checked).toBe(false);

    const threshInput = screen.getByLabelText(/Cooling.*outdoor threshold/i) as HTMLInputElement;
    expect(threshInput.value).toBe("");
  });
});
