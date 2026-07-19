import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import EcoSuspendModal from "./EcoSuspendModal";
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
    eco_mode_enabled: true,
    ...over,
  };
}

const THERMOSTATS = [tc(), tc({ thermostat_entity_id: "climate.down", name: "Downstairs" })];

describe("EcoSuspendModal (Issue #500)", () => {
  beforeEach(() => vi.clearAllMocks());

  it("defaults the picker to the first thermostat and lets the user choose", () => {
    render(<EcoSuspendModal thermostats={THERMOSTATS} onClose={() => {}} onChanged={() => {}} />);
    const picker = screen.getByLabelText(/Thermostat/i);
    expect(picker).toHaveValue("climate.up");
    fireEvent.change(picker, { target: { value: "climate.down" } });
    expect(picker).toHaveValue("climate.down");
  });

  it("pre-selects initialThermostat when opened from a scoped control", () => {
    render(
      <EcoSuspendModal
        thermostats={THERMOSTATS}
        initialThermostat="climate.down"
        onClose={() => {}}
        onChanged={() => {}}
      />
    );
    expect(screen.getByLabelText(/Thermostat/i)).toHaveValue("climate.down");
  });

  it("suspends the chosen thermostat with the picked resume datetime", async () => {
    vi.mocked(api.setEcoSuspend).mockResolvedValue({
      thermostat_entity_id: "climate.down",
      resume_at: "2099-06-01T22:00:00+00:00",
    });
    const onChanged = vi.fn();
    const onClose = vi.fn();
    render(<EcoSuspendModal thermostats={THERMOSTATS} onClose={onClose} onChanged={onChanged} />);
    fireEvent.change(screen.getByLabelText(/Thermostat/i), {
      target: { value: "climate.down" },
    });
    fireEvent.change(screen.getByLabelText(/Resume Eco at/i), {
      target: { value: "2099-06-01T18:00" },
    });
    fireEvent.click(screen.getByText("Suspend Eco"));
    await waitFor(() =>
      expect(api.setEcoSuspend).toHaveBeenCalledWith(
        "climate.down",
        new Date("2099-06-01T18:00").toISOString()
      )
    );
    expect(onChanged).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it("rejects a missing resume datetime without calling the API", async () => {
    render(<EcoSuspendModal thermostats={THERMOSTATS} onClose={() => {}} onChanged={() => {}} />);
    fireEvent.click(screen.getByText("Suspend Eco"));
    expect(await screen.findByText(/choose when Eco Mode should resume/i)).toBeInTheDocument();
    expect(api.setEcoSuspend).not.toHaveBeenCalled();
  });

  it("rejects a past resume datetime without calling the API", async () => {
    render(<EcoSuspendModal thermostats={THERMOSTATS} onClose={() => {}} onChanged={() => {}} />);
    fireEvent.change(screen.getByLabelText(/Resume Eco at/i), {
      target: { value: "2001-01-01T00:00" },
    });
    fireEvent.click(screen.getByText("Suspend Eco"));
    expect(await screen.findByText(/must be in the future/i)).toBeInTheDocument();
    expect(api.setEcoSuspend).not.toHaveBeenCalled();
  });

  it("shows the active suspension with update + resume-now actions", async () => {
    const suspended = tc({ eco_suspend_until: "2099-12-25T10:00:00+00:00" });
    vi.mocked(api.clearEcoSuspend).mockResolvedValue({
      thermostat_entity_id: "climate.up",
      resume_at: null,
    });
    const onChanged = vi.fn();
    render(
      <EcoSuspendModal
        thermostats={[suspended]}
        initialThermostat="climate.up"
        onClose={() => {}}
        onChanged={onChanged}
      />
    );
    expect(screen.getByText(/Eco Mode suspended/i)).toBeInTheDocument();
    expect(screen.getByText("Update suspension")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Resume Eco now"));
    await waitFor(() => expect(api.clearEcoSuspend).toHaveBeenCalledWith("climate.up"));
    expect(onChanged).toHaveBeenCalled();
  });

  it("hints when the selected thermostat has Eco off", () => {
    render(
      <EcoSuspendModal
        thermostats={[tc({ eco_mode_enabled: false })]}
        onClose={() => {}}
        onChanged={() => {}}
      />
    );
    expect(screen.getByText(/Eco Mode is not enabled on this thermostat/i)).toBeInTheDocument();
  });

  it("requires a thermostat selection when none are available", async () => {
    render(<EcoSuspendModal thermostats={[]} onClose={() => {}} onChanged={() => {}} />);
    fireEvent.click(screen.getByText("Suspend Eco"));
    expect(await screen.findByText(/choose a thermostat/i)).toBeInTheDocument();
    expect(api.setEcoSuspend).not.toHaveBeenCalled();
  });

  it("surfaces a resume-now failure as an inline error", async () => {
    vi.mocked(api.clearEcoSuspend).mockRejectedValue(new Error("boom"));
    render(
      <EcoSuspendModal
        thermostats={[tc({ eco_suspend_until: "2099-12-25T10:00:00+00:00" })]}
        onClose={() => {}}
        onChanged={() => {}}
      />
    );
    fireEvent.click(screen.getByRole("button", { name: /Resume Eco now/i }));
    expect(await screen.findByText("boom")).toBeInTheDocument();
  });

  it("closes on backdrop click without saving", () => {
    const onClose = vi.fn();
    const { container } = render(
      <EcoSuspendModal thermostats={THERMOSTATS} onClose={onClose} onChanged={() => {}} />
    );
    fireEvent.click(container.querySelector(".modal-backdrop") as HTMLElement);
    expect(onClose).toHaveBeenCalled();
    expect(api.setEcoSuspend).not.toHaveBeenCalled();
  });

  it("surfaces an API failure as an inline error", async () => {
    vi.mocked(api.setEcoSuspend).mockRejectedValue(new Error("resume_at must be in the future"));
    render(<EcoSuspendModal thermostats={THERMOSTATS} onClose={() => {}} onChanged={() => {}} />);
    fireEvent.change(screen.getByLabelText(/Resume Eco at/i), {
      target: { value: "2099-06-01T18:00" },
    });
    fireEvent.click(screen.getByText("Suspend Eco"));
    expect(await screen.findByText(/resume_at must be in the future/i)).toBeInTheDocument();
  });
});
