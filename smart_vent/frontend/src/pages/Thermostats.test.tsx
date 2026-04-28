import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import Thermostats from "./Thermostats";
import * as api from "../api";


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
  },
];

describe("Thermostats Page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.getThermostats as any).mockResolvedValue(mockThermostats);
    (api.getHAEntities as any).mockResolvedValue([
        { entity_id: "climate.hallway", friendly_name: "Hallway" }
    ]);
    (api.downloadBackup as any).mockResolvedValue(undefined);
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
    (api.createThermostat as any).mockResolvedValue({ thermostat_entity_id: "climate.hallway", name: "Hallway" });
    render(<Thermostats />);
    fireEvent.click(await screen.findByText("+ Register thermostat"));

    const nameInput = await screen.findByLabelText(/Friendly name/i, { selector: "#add-thermo-name" });
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
        name: "Hallway HVAC"
      });
    });
  });

  it("updates thermostat settings", async () => {
    (api.updateThermostat as any).mockResolvedValue({});
    render(<Thermostats />);

    const nameInput = await screen.findByLabelText(/Friendly name/i, { selector: "#thermo-climate\\.test-name" });
    fireEvent.change(nameInput, { target: { value: "Updated Name" } });

    const card = nameInput.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByText("Save changes"));

    await waitFor(() => {
      expect(api.updateThermostat).toHaveBeenCalledWith("climate.test", expect.objectContaining({
        name: "Updated Name"
      }));
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

    expect(await screen.findByText("Min setpoint must be less than max setpoint")).toBeInTheDocument();
  });

  it("handles backup download", async () => {
    render(<Thermostats />);
    const downloadBtn = await screen.findByText("Download backup");
    fireEvent.click(downloadBtn);
    expect(api.downloadBackup).toHaveBeenCalled();
  });

  it("handles restore from backup", async () => {
    window.confirm = vi.fn().mockReturnValue(true);
    (api.restoreBackup as any).mockResolvedValue({});
    const { container } = render(<Thermostats />);

    // Wait for loading to finish
    await screen.findByText("Main HVAC");

    const fileInput = container.querySelector('#restore-backup-input') as HTMLInputElement;
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
