/**
 * Behavioural coverage for the parts of `Thermostats.tsx` the main suite never
 * reaches: the registration modal's refusals and dismissal paths, the nullable
 * / fallback rendering of a sparsely-populated ThermostatConfig, the clamped
 * drift-interval input, and every catch block that turns a rejected API call
 * into something the operator can read.
 *
 * All of it drives the rendered page — no private helper is invoked directly —
 * so a covered branch is a branch a user can actually reach.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { ecoThermostatDefaults } from "../testFixtures";
import { act, render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import Thermostats from "./Thermostats";
import * as api from "../api";

vi.mock("../api");

const baseConfig: api.ThermostatConfig = {
  thermostat_entity_id: "climate.test",
  name: "Main HVAC",
  default_temp: 72,
  min_setpoint: 60,
  max_setpoint: 80,
  deadband: 0.5,
  max_vent_closed_min: 60,
  total_vents_count: 12,
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
};

/**
 * A row written before several fields existed — every nullable column comes
 * back null. Typed through `unknown` on purpose: the API's TypeScript type
 * says these are non-null, but a legacy SQLite row is what actually arrives,
 * and the card's `??` fallbacks exist for exactly that.
 */
const sparseConfig = {
  ...baseConfig,
  name: "",
  default_temp: null,
  total_vents_count: null,
  min_open_vents_fraction: null,
  min_cycle_runtime_min: null,
  overflow_during_min_runtime: null,
  reconciliation_interval_min: null,
  vacation_hvac_mode: null,
} as unknown as api.ThermostatConfig;

/** Wait for the cards to commit AND their mount effects to flush (see #597). */
const cardsSettled = async (name: string | RegExp = "Main HVAC") => {
  await screen.findByText(name);
  await act(async () => {});
};

const cardOf = (el: HTMLElement) => el.closest(".card") as HTMLElement;

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getRooms).mockResolvedValue([]);
  vi.mocked(api.getThermostats).mockResolvedValue([baseConfig]);
  vi.mocked(api.getSensorStaleness).mockResolvedValue({ stale_after_min: 30 });
  vi.mocked(api.getOutsideTempEntity).mockResolvedValue({ entity_id: null, current_value: null });
  vi.mocked(api.getHAEntities).mockResolvedValue([
    { entity_id: "climate.hallway", friendly_name: "Hallway", state: "" },
  ]);
  vi.mocked(api.updateThermostat).mockResolvedValue(baseConfig);
});

// ---------------------------------------------------------------------------
// Registration modal
// ---------------------------------------------------------------------------

describe("Thermostats — registration modal", () => {
  const openModal = async () => {
    render(<Thermostats />);
    fireEvent.click(await screen.findByText("+ Register thermostat"));
    return await screen.findByText("Register Thermostat");
  };

  const pickHallway = async () => {
    const picker = screen.getByPlaceholderText(/Search thermostats/i);
    fireEvent.focus(picker);
    fireEvent.change(picker, { target: { value: "hallway" } });
    fireEvent.mouseDown(await screen.findByText("Hallway"));
  };

  it("refuses to register with an entity but no friendly name", async () => {
    await openModal();
    await pickHallway();
    // The name field is deliberately left blank.
    fireEvent.click(screen.getByRole("button", { name: "Register" }));

    expect(await screen.findByText("Friendly name is required")).toBeInTheDocument();
    expect(api.createThermostat).not.toHaveBeenCalled();
  });

  it("refuses a whitespace-only friendly name", async () => {
    await openModal();
    await pickHallway();
    fireEvent.change(screen.getByLabelText(/Friendly name/i, { selector: "#add-thermo-name" }), {
      target: { value: "   " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Register" }));

    expect(await screen.findByText("Friendly name is required")).toBeInTheDocument();
    expect(api.createThermostat).not.toHaveBeenCalled();
  });

  it("lets the chosen entity be cleared again, which re-arms the entity refusal", async () => {
    await openModal();
    await pickHallway();
    expect(screen.getByText("climate.hallway")).toBeInTheDocument();

    // The ✓ chip's × button un-picks the entity.
    fireEvent.click(screen.getByRole("button", { name: "×" }));
    expect(screen.queryByText("climate.hallway")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Register" }));
    expect(await screen.findByText("Select a thermostat entity")).toBeInTheDocument();
  });

  it("carries the bypass-damper tick into the create payload", async () => {
    vi.mocked(api.createThermostat).mockResolvedValue({
      ...baseConfig,
      thermostat_entity_id: "climate.hallway",
      name: "Hallway HVAC",
    });
    await openModal();
    await pickHallway();
    fireEvent.change(screen.getByLabelText(/Friendly name/i, { selector: "#add-thermo-name" }), {
      target: { value: "Hallway HVAC" },
    });
    fireEvent.change(document.getElementById("add-thermo-total-vents") as HTMLInputElement, {
      target: { value: "8" },
    });

    const damper = document.getElementById("add-thermo-bypass-damper") as HTMLInputElement;
    expect(damper.checked).toBe(false);
    fireEvent.click(damper);
    expect(damper.checked).toBe(true);

    // AirflowConfigBanner fetches the same endpoint on mount, so count the
    // page's own refetch as a delta rather than an absolute.
    const before = vi.mocked(api.getThermostats).mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Register" }));
    await waitFor(() =>
      expect(api.createThermostat).toHaveBeenCalledWith({
        thermostat_entity_id: "climate.hallway",
        name: "Hallway HVAC",
        total_vents_count: 8,
        has_bypass_damper: true,
      })
    );
    // A successful registration closes the modal and refetches the list.
    await waitFor(() => expect(screen.queryByText("Register Thermostat")).toBeNull());
    expect(vi.mocked(api.getThermostats).mock.calls.length).toBe(before + 1);
  });

  it("surfaces the backend's rejection message and keeps the modal open", async () => {
    vi.mocked(api.createThermostat).mockRejectedValue(new Error("Entity already registered"));
    await openModal();
    await pickHallway();
    fireEvent.change(screen.getByLabelText(/Friendly name/i, { selector: "#add-thermo-name" }), {
      target: { value: "Hallway HVAC" },
    });
    fireEvent.change(document.getElementById("add-thermo-total-vents") as HTMLInputElement, {
      target: { value: "8" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Register" }));

    expect(await screen.findByText("Entity already registered")).toBeInTheDocument();
    expect(screen.getByText("Register Thermostat")).toBeInTheDocument();
    // The finally block re-enables the button so the user can retry.
    expect(screen.getByRole("button", { name: "Register" })).not.toBeDisabled();
  });

  it("falls back to a generic message when the rejection is not an Error", async () => {
    vi.mocked(api.createThermostat).mockRejectedValue("network went away");
    await openModal();
    await pickHallway();
    fireEvent.change(screen.getByLabelText(/Friendly name/i, { selector: "#add-thermo-name" }), {
      target: { value: "Hallway HVAC" },
    });
    fireEvent.change(document.getElementById("add-thermo-total-vents") as HTMLInputElement, {
      target: { value: "8" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Register" }));

    expect(await screen.findByText("Save failed")).toBeInTheDocument();
  });

  it("closes on Cancel and on a backdrop click, without registering anything", async () => {
    await openModal();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText("Register Thermostat")).toBeNull();

    fireEvent.click(screen.getByText("+ Register thermostat"));
    await screen.findByText("Register Thermostat");
    // A click bubbling out of the dialog body must not dismiss it…
    fireEvent.click(screen.getByText("Register Thermostat"));
    expect(screen.getByText("Register Thermostat")).toBeInTheDocument();
    // …only one that lands on the backdrop itself.
    fireEvent.click(document.querySelector(".modal-backdrop") as HTMLElement);
    expect(screen.queryByText("Register Thermostat")).toBeNull();
    expect(api.createThermostat).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Card — save / delete failure paths
// ---------------------------------------------------------------------------

describe("Thermostats — card failure paths", () => {
  it("refuses to save a card whose friendly name has been cleared", async () => {
    render(<Thermostats />);
    await cardsSettled();

    const nameInput = screen.getByLabelText(/Friendly name/i, {
      selector: "#thermo-climate\\.test-name",
    });
    fireEvent.change(nameInput, { target: { value: "   " } });
    fireEvent.click(within(cardOf(nameInput)).getByText("Save changes"));

    expect(await screen.findByText("Friendly name is required")).toBeInTheDocument();
    expect(api.updateThermostat).not.toHaveBeenCalled();
  });

  it("shows '(unnamed)' in the header and names the entity in the remove dialog", async () => {
    vi.mocked(api.getThermostats).mockResolvedValue([sparseConfig]);
    render(<Thermostats />);
    await cardsSettled("(unnamed)");

    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    const dialog = await screen.findByTestId("confirm-dialog");
    // With no friendly name the confirmation falls back to the entity id, so
    // the prompt still says which thermostat is about to go.
    expect(within(dialog).getByText(/climate\.test/)).toBeInTheDocument();
  });

  it("surfaces an Error rejection from the save", async () => {
    vi.mocked(api.updateThermostat).mockRejectedValue(new Error("min_setpoint out of range"));
    render(<Thermostats />);
    await cardsSettled();

    const card = cardOf(await screen.findByLabelText(/^Total vent count$/));
    fireEvent.click(within(card).getByText("Save changes"));

    expect(await screen.findByText("min_setpoint out of range")).toBeInTheDocument();
    expect(within(card).queryByText("Saved!")).toBeNull();
  });

  it("falls back to a generic message when the save rejects with a non-Error", async () => {
    vi.mocked(api.updateThermostat).mockRejectedValue({ status: 502 });
    render(<Thermostats />);
    await cardsSettled();

    const card = cardOf(await screen.findByLabelText(/^Total vent count$/));
    fireEvent.click(within(card).getByText("Save changes"));

    expect(await screen.findByText("Save failed")).toBeInTheDocument();
  });

  it("surfaces an Error rejection from the delete and keeps the card", async () => {
    vi.mocked(api.deleteThermostat).mockRejectedValue(new Error("Thermostat is in use"));
    render(<Thermostats />);
    await cardsSettled();

    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    const dialog = await screen.findByTestId("confirm-dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove" }));

    expect(await screen.findByText("Thermostat is in use")).toBeInTheDocument();
    expect(screen.getByText("Main HVAC")).toBeInTheDocument();
  });

  it("falls back to a generic message when the delete rejects with a non-Error", async () => {
    vi.mocked(api.deleteThermostat).mockRejectedValue("gone");
    render(<Thermostats />);
    await cardsSettled();

    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    const dialog = await screen.findByTestId("confirm-dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove" }));

    expect(await screen.findByText("Delete failed")).toBeInTheDocument();
  });

  it("ignores a failed outside-temperature lookup, leaving Eco gated", async () => {
    // The GET is best-effort; a rejection must not blank the page. It must
    // also not be mistaken for "a sensor is configured".
    vi.mocked(api.getOutsideTempEntity).mockRejectedValue(new Error("HA unreachable"));
    render(<Thermostats />);
    await cardsSettled();

    const ecoToggle = screen.getByLabelText(/Enable Eco Mode for this thermostat/i);
    expect(ecoToggle).toBeDisabled();
    expect(screen.getByText(/Add a free weather integration such as/i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// "Saved!" badge lifecycle
// ---------------------------------------------------------------------------

describe("Thermostats — the Saved! badge", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("clears itself after 2s, and a second save restarts rather than stacks the timer", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<Thermostats />);
    await cardsSettled();

    const card = cardOf(await screen.findByLabelText(/^Total vent count$/));
    fireEvent.click(within(card).getByText("Save changes"));
    await within(card).findByText("Saved!");

    // Second save 1s in: the first timer is cancelled, so the badge must not
    // vanish at the ORIGINAL deadline — it gets a fresh 2s from here.
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    fireEvent.click(within(card).getByText("Save changes"));
    await within(card).findByText("Saved!");

    await act(async () => {
      vi.advanceTimersByTime(1500);
    });
    expect(within(card).getByText("Saved!")).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(600);
    });
    expect(within(card).queryByText("Saved!")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Nullable / fallback rendering
// ---------------------------------------------------------------------------

describe("Thermostats — a legacy config with null columns", () => {
  beforeEach(() => {
    vi.mocked(api.getThermostats).mockResolvedValue([sparseConfig]);
  });

  it("renders every nullable field through its documented fallback", async () => {
    render(<Thermostats />);
    await cardsSettled("(unnamed)");

    // Nullable numerics render blank, never the string "null".
    expect(document.getElementById("thermo-climate.test-default_temp")).toHaveValue(null);
    expect(document.getElementById("thermo-climate.test-total-vents")).toHaveValue(null);
    expect(document.getElementById("thermo-climate.test-min_cycle_runtime_min")).toHaveDisplayValue(
      ""
    );

    // Booleans and enums fall back to their defaults.
    const overflow = screen.getByLabelText(/Redirect surplus air to other rooms/i);
    expect(overflow).toBeChecked();
    expect(overflow).toBeDisabled(); // min-runtime hold is null → treated as 0
    expect(screen.getByLabelText(/Vacation HVAC mode/i)).toHaveValue("single");

    // The airflow floor falls back to one third, in both the label and slider.
    expect(screen.getByText("33%")).toBeInTheDocument();
    expect(document.getElementById("thermo-climate.test-fraction")).toHaveValue("0.333");

    // Drift correction falls back to 0 (= disabled).
    expect(screen.getByLabelText(/Drift correction interval/i)).toHaveValue(0);
  });

  it("accepts an edit on each fallback field and posts a real value", async () => {
    render(<Thermostats />);
    await cardsSettled("(unnamed)");

    fireEvent.change(
      screen.getByLabelText(/Friendly name/i, {
        selector: "#thermo-climate\\.test-name",
      }),
      { target: { value: "Legacy HVAC" } }
    );
    fireEvent.change(document.getElementById("thermo-climate.test-default_temp")!, {
      target: { value: "70" },
    });
    fireEvent.change(document.getElementById("thermo-climate.test-total-vents")!, {
      target: { value: "9" },
    });
    fireEvent.change(screen.getByLabelText(/Min cycle runtime/i), { target: { value: "10" } });
    fireEvent.change(document.getElementById("thermo-climate.test-fraction")!, {
      target: { value: "0.5" },
    });
    fireEvent.change(screen.getByLabelText(/Drift correction interval/i), {
      target: { value: "30" },
    });
    fireEvent.change(screen.getByLabelText(/Vacation HVAC mode/i), { target: { value: "range" } });

    const card = cardOf(screen.getByLabelText(/^Total vent count$/));
    fireEvent.click(within(card).getByText("Save changes"));

    await waitFor(() =>
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({
          name: "Legacy HVAC",
          default_temp: 70,
          total_vents_count: 9,
          min_cycle_runtime_min: 10,
          min_open_vents_fraction: 0.5,
          reconciliation_interval_min: 30,
          vacation_hvac_mode: "range",
        })
      )
    );
  });

  it("clears the default presence temp back to null when the field is emptied", async () => {
    render(<Thermostats />);
    await cardsSettled("(unnamed)");

    const field = document.getElementById("thermo-climate.test-default_temp")!;
    fireEvent.change(field, { target: { value: "70" } });
    expect(field).toHaveValue(70);
    fireEvent.change(field, { target: { value: "" } });

    fireEvent.change(
      screen.getByLabelText(/Friendly name/i, {
        selector: "#thermo-climate\\.test-name",
      }),
      { target: { value: "Legacy HVAC" } }
    );
    const card = cardOf(screen.getByLabelText(/^Total vent count$/));
    fireEvent.click(within(card).getByText("Save changes"));

    await waitFor(() =>
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({ default_temp: null })
      )
    );
  });
});

// ---------------------------------------------------------------------------
// Numeric coercion on the card's own inputs
// ---------------------------------------------------------------------------

describe("Thermostats — numeric coercion", () => {
  it("coerces a blanked safety field to 0 rather than NaN", async () => {
    render(<Thermostats />);
    await cardsSettled();

    const maxClosed = screen.getByLabelText(/Max vent closed/i);
    fireEvent.change(maxClosed, { target: { value: "" } });

    fireEvent.click(within(cardOf(maxClosed)).getByText("Save changes"));
    await waitFor(() =>
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({ max_vent_closed_min: 0 })
      )
    );
    const [, payload] = vi.mocked(api.updateThermostat).mock.calls[0];
    expect(Number.isNaN(payload.max_vent_closed_min as number)).toBe(false);
  });

  it("keeps a zero cooling lockout as 0, and re-blanking it as null", async () => {
    vi.mocked(api.getThermostats).mockResolvedValue([
      { ...baseConfig, cooling_lockout_below_f: 55 },
    ]);
    render(<Thermostats />);
    await cardsSettled();

    // A stored lockout initialises the field (the non-null derive branch).
    const lockout = screen.getByLabelText(/Cooling lockout/i);
    expect(lockout).toHaveValue(55);

    // 0 is falsy but a real setting — it must not be coerced away.
    fireEvent.change(lockout, { target: { value: "0" } });
    fireEvent.click(within(cardOf(lockout)).getByText("Save changes"));
    await waitFor(() =>
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({ cooling_lockout_below_f: 0 })
      )
    );
  });

  it("floors the total vent count at 1 when 0 is typed, and nulls it when blanked", async () => {
    render(<Thermostats />);
    await cardsSettled();

    const totalVents = screen.getByLabelText(/^Total vent count$/);
    fireEvent.change(totalVents, { target: { value: "0" } });
    expect(totalVents).toHaveValue(1);

    fireEvent.change(totalVents, { target: { value: "" } });
    fireEvent.click(within(cardOf(totalVents)).getByText("Save changes"));
    await waitFor(() =>
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({ total_vents_count: null })
      )
    );
  });

  it("clamps the drift interval to the cycle timeout and coerces a blank to 0", async () => {
    render(<Thermostats />);
    await cardsSettled();

    // cycle_timeout_hours = 2 → the ceiling is 120 min, and the hint says so.
    const drift = screen.getByLabelText(/Drift correction interval/i);
    expect(drift).toHaveAttribute("max", "120");
    expect(screen.getByText(/Cannot exceed the cycle timeout \(\s*120 min\)/)).toBeInTheDocument();

    fireEvent.change(drift, { target: { value: "9999" } });
    expect(drift).toHaveValue(120);

    fireEvent.change(drift, { target: { value: "45" } });
    expect(drift).toHaveValue(45);

    fireEvent.change(drift, { target: { value: "" } });
    expect(drift).toHaveValue(0);

    fireEvent.click(within(cardOf(drift)).getByText("Save changes"));
    await waitFor(() =>
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({ reconciliation_interval_min: 0 })
      )
    );
  });
});

// ---------------------------------------------------------------------------
// Vacation-mode test buttons
// ---------------------------------------------------------------------------

describe("Thermostats — vacation auto-mode test failures", () => {
  beforeEach(() => {
    vi.mocked(api.getThermostats).mockResolvedValue([
      { ...baseConfig, vacation_hvac_mode: "range" as const },
    ]);
  });

  it("badges a failed test in red with the backend's message", async () => {
    vi.mocked(api.testVacationMode).mockRejectedValue(new Error("heat_cool unsupported"));
    render(<Thermostats />);

    fireEvent.click(await screen.findByRole("button", { name: /Test auto mode/i }));

    const badge = await screen.findByText("Error: heat_cool unsupported");
    expect(badge.className).toContain("badge-red");
    // The test never became active, so the Revert button must not appear.
    expect(screen.queryByRole("button", { name: /Revert test/i })).toBeNull();
    expect(screen.getByRole("button", { name: /Test auto mode/i })).not.toBeDisabled();
  });

  it("falls back to a generic message when the test rejects with a non-Error", async () => {
    vi.mocked(api.testVacationMode).mockRejectedValue("nope");
    render(<Thermostats />);

    fireEvent.click(await screen.findByRole("button", { name: /Test auto mode/i }));
    expect(await screen.findByText("Error: Test failed")).toBeInTheDocument();
  });

  it("badges a failed revert in red and leaves the test active", async () => {
    vi.mocked(api.testVacationMode).mockResolvedValue({
      ok: true,
      min_setpoint: 60,
      max_setpoint: 80,
      thermostat_state: {},
    });
    vi.mocked(api.revertVacationTest).mockRejectedValue(new Error("thermostat offline"));
    render(<Thermostats />);

    fireEvent.click(await screen.findByRole("button", { name: /Test auto mode/i }));
    const revert = await screen.findByRole("button", { name: /Revert test/i });
    // The successful test badge is green, which is what makes the red below
    // an actual signal rather than the only colour this element ever wears.
    expect(screen.getByText(/heat_cool active/).className).toContain("badge-green");

    fireEvent.click(revert);
    const badge = await screen.findByText("Error: thermostat offline");
    expect(badge.className).toContain("badge-red");
    // Still active — the engine will revert it on the next tick.
    expect(screen.getByRole("button", { name: /Revert test/i })).toBeInTheDocument();
  });

  it("falls back to a generic message when the revert rejects with a non-Error", async () => {
    vi.mocked(api.testVacationMode).mockResolvedValue({
      ok: true,
      min_setpoint: 60,
      max_setpoint: 80,
      thermostat_state: {},
    });
    vi.mocked(api.revertVacationTest).mockRejectedValue(0);
    render(<Thermostats />);

    fireEvent.click(await screen.findByRole("button", { name: /Test auto mode/i }));
    fireEvent.click(await screen.findByRole("button", { name: /Revert test/i }));
    expect(await screen.findByText("Error: Revert failed")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Sensor-staleness input coercion (#600 / #211)
// ---------------------------------------------------------------------------

describe("Thermostats — sensor-staleness input", () => {
  it("coerces a blanked threshold to 0 instead of NaN before the PUT", async () => {
    vi.mocked(api.setSensorStaleness).mockResolvedValue({ stale_after_min: 30 });
    render(<Thermostats />);

    const input = (await screen.findByLabelText(/^Minutes$/i)) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "" } });
    expect(input).toHaveValue(0);

    fireEvent.click(within(cardOf(input)).getByRole("button", { name: /^Save$/i }));
    await waitFor(() => expect(api.setSensorStaleness).toHaveBeenCalledWith(0));
  });
});
