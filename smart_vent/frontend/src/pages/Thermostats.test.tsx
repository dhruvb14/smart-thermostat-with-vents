import { StrictMode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { ecoThermostatDefaults, ecoRoomDefaults } from "../testFixtures";
import { act, render, screen, fireEvent, waitFor, within } from "@testing-library/react";
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

/**
 * Resolves once the page has loaded AND the freshly mounted cards' passive
 * effects have flushed.
 *
 * A `findBy*` only waits on the DOM. React commits the card and then schedules
 * its passive effects as a SEPARATE host task, and RTL drops the act
 * environment while awaiting — so a query can legitimately resolve with a
 * card's mount effects still pending, and the next `fireEvent` (which re-enters
 * act) flushes them AFTER the edit it just made. That is the #597 flake.
 *
 * So: wait for the card to commit, then drain React's pending work. `act` is
 * the documented flush primitive — not a timer, and not a proxy. An earlier
 * draft waited on `getOutsideTempEntity` having been called instead; that was
 * sound only by the incidental fact that `OutsideTempPicker` and the cards sit
 * behind the same `loading` gate and so share a commit. Hoisting the picker
 * above that gate would have satisfied the wait with the picker's own call
 * while the cards' effects were still pending, silently restoring the race
 * with no test failing. Flushing directly cannot degrade that way.
 */
const cardsSettled = async (name = "Main HVAC") => {
  await screen.findByText(name);
  await act(async () => {});
};

describe("Thermostats Page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getRooms).mockResolvedValue([]);
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

  it("requires confirmation before removing a thermostat, naming it", async () => {
    vi.mocked(api.deleteThermostat).mockResolvedValue({ deleted: "climate.test" });
    render(<Thermostats />);

    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));

    const dialog = await screen.findByTestId("confirm-dialog");
    expect(within(dialog).getByText(/Main HVAC/)).toBeInTheDocument();
    expect(api.deleteThermostat).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() => {
      expect(api.deleteThermostat).toHaveBeenCalledWith("climate.test");
    });
  });

  it("does not remove a thermostat when the confirmation dialog is cancelled", async () => {
    render(<Thermostats />);

    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));

    const dialog = await screen.findByTestId("confirm-dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(screen.queryByTestId("confirm-dialog")).not.toBeInTheDocument();
    expect(api.deleteThermostat).not.toHaveBeenCalled();
  });

  it("disables the airflow fraction slider when the bypass-damper checkbox is ticked (Issue #213)", async () => {
    vi.mocked(api.updateThermostat).mockResolvedValue({} as api.ThermostatConfig);
    render(<Thermostats />);
    await cardsSettled();

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
    await cardsSettled();

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
    // The echo deliberately differs from what the user typed: the backend
    // normalised min_setpoint 60 → 61. Without a difference the re-derive is
    // unobservable and this test would pass even with `onSaved` deleted or the
    // whole content comparison removed — it would guard nothing (#597).
    vi.mocked(api.updateThermostat).mockResolvedValue({
      ...mockThermostats[0],
      name: "Renamed HVAC",
      min_setpoint: 61,
    });

    const { rerender } = render(
      <UnitContext.Provider value={buildUnitContext("F")}>
        <Thermostats />
      </UnitContext.Provider>
    );
    await cardsSettled();

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

    // The form adopted the server's echo, which is what `onSaved` exists for:
    // a later re-derive must come from the just-saved values, never the stale
    // page-load config that would silently regress the DB on re-save (#293).
    await waitFor(() => expect(screen.getByLabelText(/Min setpoint/i)).toHaveDisplayValue("61"));

    // Simulate App re-rendering with fresh unit-context identities (the #293
    // trigger — e.g. toggling System/Dev). The form must still show the
    // just-SAVED name: it adopted the server echo when the content changed at
    // save time, and an identity-only change must NOT re-derive at all (#597).
    // Do not "restore" an identity-keyed reset to make this pass — that is the
    // bug, and this test passes either way.
    rerender(
      <UnitContext.Provider value={buildUnitContext("F")}>
        <Thermostats />
      </UnitContext.Provider>
    );

    expect(screen.getByDisplayValue("Renamed HVAC")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("Main HVAC")).not.toBeInTheDocument();
  });

  it("keeps an in-progress edit when a sibling card is removed and the page refetches (#597, #293)", async () => {
    // load() re-runs after a delete and hands every SURVIVING card a brand-new
    // ThermostatConfig object with byte-identical content. Cards are keyed by
    // entity id, so the survivor is not remounted — it is a live form being
    // re-derived under the user. Re-deriving on object identity silently threw
    // the typing away; re-deriving on content leaves it alone. Same class as
    // #293, one path over.
    const spare: api.ThermostatConfig = {
      ...mockThermostats[0],
      thermostat_entity_id: "climate.spare",
      name: "Spare HVAC",
    };
    vi.mocked(api.getThermostats).mockResolvedValue([mockThermostats[0], spare]);
    vi.mocked(api.deleteThermostat).mockResolvedValue({ deleted: "climate.spare" });
    vi.mocked(api.updateThermostat).mockResolvedValue({} as api.ThermostatConfig);

    render(<Thermostats />);
    await cardsSettled();

    const nameInput = screen.getByLabelText(/Friendly name/i, {
      selector: "#thermo-climate\\.test-name",
    }) as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: "Edited, not yet saved" } });
    expect(nameInput.value).toBe("Edited, not yet saved");

    // A real refetch returns structurally identical data behind fresh identities.
    vi.mocked(api.getThermostats).mockResolvedValue([{ ...mockThermostats[0] }]);

    const spareCard = screen.getByText("climate.spare").closest(".card") as HTMLElement;
    fireEvent.click(within(spareCard).getByRole("button", { name: "Remove" }));
    const dialog = await screen.findByTestId("confirm-dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove" }));

    await waitFor(() => expect(api.deleteThermostat).toHaveBeenCalledWith("climate.spare"));
    await waitFor(() => expect(screen.queryByText("climate.spare")).toBeNull());

    // Settle React fully before asserting. The old reset was queued in a passive
    // effect, which lands a host task AFTER the refetch's DOM update — reading
    // the input straight out of the waitFor above passed by luck. `act` is the
    // documented way to drain pending effects; it is a flush, not a timer.
    await act(async () => {});

    // The edit survived the refetch, and Save posts it — the payload is the
    // thing #293 is about.
    const survivor = screen.getByDisplayValue("Edited, not yet saved").closest(".card")!;
    fireEvent.click(within(survivor as HTMLElement).getByText("Save changes"));

    await waitFor(() =>
      expect(api.updateThermostat).toHaveBeenCalledWith(
        "climate.test",
        expect.objectContaining({ name: "Edited, not yet saved" })
      )
    );
  });

  it("keeps an in-progress edit when Eco is suspended from that same card (#597, #500)", async () => {
    // The most reachable version of the bug: the Suspend Eco button lives in
    // the header of the card being edited, and EcoSuspendModal's onChanged is
    // wired to load(). The refetch moves exactly one field — eco_suspend_until
    // — which the form does not own (the header reads it off `config`, and the
    // PUT allowlist refuses it), so it must not re-derive the form.
    const ecoOn = { ...mockThermostats[0], eco_mode_enabled: true };
    vi.mocked(api.getThermostats).mockResolvedValue([ecoOn]);
    vi.mocked(api.setEcoSuspend).mockResolvedValue({
      thermostat_entity_id: "climate.test",
      resume_at: "2099-12-25T10:00:00+00:00",
    });
    vi.mocked(api.updateThermostat).mockResolvedValue({} as api.ThermostatConfig);

    render(<Thermostats />);
    await cardsSettled();

    const nameInput = screen.getByLabelText(/Friendly name/i, {
      selector: "#thermo-climate\\.test-name",
    }) as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: "Edited, not yet saved" } });
    expect(nameInput.value).toBe("Edited, not yet saved");

    // The refetch after the suspend differs ONLY in eco_suspend_until.
    vi.mocked(api.getThermostats).mockResolvedValue([
      { ...ecoOn, eco_suspend_until: "2099-12-25T10:00:00+00:00" },
    ]);

    const card = nameInput.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByRole("button", { name: /Suspend Eco/i }));
    fireEvent.change(screen.getByLabelText(/Resume Eco at/i), {
      target: { value: "2099-12-25T10:00" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Suspend Eco$/i }));

    await waitFor(() => expect(api.setEcoSuspend).toHaveBeenCalled());
    // The suspension landed — the header now reads it straight off `config`.
    await screen.findByText(/Eco suspended until/);
    await act(async () => {});

    expect(nameInput.value).toBe("Edited, not yet saved");
  });

  it("still re-derives the form when the unit context genuinely changes (#123 guard for #597)", async () => {
    // A counterweight, NOT a reproduction: this passes on the pre-fix code too.
    // It exists to reject the two wrong fixes — deriving once and never again
    // (the input would read 60 under a °C label), and gating the re-derive
    // behind a dirty flag (the typed 62 would survive the flip). App resolves
    // /api/settings after mount, so a card really can mount under the default
    // °F context on a °C install; the form has to be re-derived even mid-edit
    // or a °F number sits under a °C label and the save round-trips it (#231).
    // The typed 62 below is load-bearing for the dirty-flag case specifically.
    const { rerender } = render(
      <UnitContext.Provider value={buildUnitContext("F")}>
        <Thermostats />
      </UnitContext.Provider>
    );
    await cardsSettled();

    const minF = screen.getByLabelText(/Min setpoint \(°F\)/i) as HTMLInputElement;
    fireEvent.change(minF, { target: { value: "62" } });
    expect(minF.value).toBe("62");

    rerender(
      <UnitContext.Provider value={buildUnitContext("C")}>
        <Thermostats />
      </UnitContext.Provider>
    );

    const minC = screen.getByLabelText(/Min setpoint \(°C\)/i) as HTMLInputElement;
    // toDisplay(60) = 15.6 °C — the stored value, not the typed 62.
    expect(parseFloat(minC.value)).toBeCloseTo(15.6, 1);
  });

  it("re-derives correctly under StrictMode's double render (#597)", async () => {
    // main.tsx wraps the app in <StrictMode>, which renders every component
    // TWICE and discards the first pass. Deriving during render has to survive
    // that, so this pins two things under the mode production actually runs in:
    // it CONVERGES (an identity comparison instead of `sameDerivedForm` throws
    // "Too many re-renders", since `derivedForm` is a fresh object each pass —
    // verified), and it re-derives to the right value after a content change.
    //
    // Scope, stated honestly: this is not a reproduction of #597 — it passes on
    // the pre-fix code — and it does NOT discriminate a ref baseline from a
    // state one; both were measured to behave identically here. Driving a real
    // content change (the unit flip below) is what gives it any power at all;
    // editing and saving under StrictMode would pass on any implementation.
    const { rerender } = render(
      <StrictMode>
        <UnitContext.Provider value={buildUnitContext("F")}>
          <Thermostats />
        </UnitContext.Provider>
      </StrictMode>
    );
    await cardsSettled();

    const minF = screen.getByLabelText(/Min setpoint \(°F\)/i) as HTMLInputElement;
    expect(minF.value).toBe("60");
    fireEvent.change(minF, { target: { value: "62" } });
    expect(minF.value).toBe("62");

    rerender(
      <StrictMode>
        <UnitContext.Provider value={buildUnitContext("C")}>
          <Thermostats />
        </UnitContext.Provider>
      </StrictMode>
    );

    // Converged on the re-derived value — not looping ("Too many re-renders"),
    // not stuck on the typed 62, not stale at 60 under a °C label.
    const minC = screen.getByLabelText(/Min setpoint \(°C\)/i) as HTMLInputElement;
    expect(parseFloat(minC.value)).toBeCloseTo(15.6, 1);
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
    await cardsSettled();

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
    await cardsSettled();

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
    await cardsSettled();

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
    await cardsSettled();

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
    await cardsSettled();

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
    await cardsSettled();

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
    await cardsSettled();

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
    vi.mocked(api.getRooms).mockResolvedValue([]);
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
    vi.mocked(api.getRooms).mockResolvedValue([]);
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
    await cardsSettled();

    const nameInput = await screen.findByLabelText(/Friendly name/i, {
      selector: "#thermo-climate\\.test-name",
    });

    // The frontend MUST send the display-unit value as-is — the backend's
    // _to_f converts °C → °F on the write boundary. Sending pre-converted
    // °F here would cause double conversion (#231).
    const minInput = screen.getByLabelText(/Min setpoint \(°C\)/i) as HTMLInputElement;
    fireEvent.change(minInput, { target: { value: "16" } });
    // Fail at the line where the damage happens. A form reset that lands inside
    // fireEvent's act() would revert this to the initial 15.6, and without this
    // the failure only surfaces later as a baffling POST body (#597).
    expect(minInput.value).toBe("16");

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
    await cardsSettled();

    const nameInput = await screen.findByLabelText(/Friendly name/i, {
      selector: "#thermo-climate\\.test-name",
    });

    // Backend _delta_to_f handles °C delta → °F delta. Frontend stays out
    // of conversion to avoid the double-conversion bug (#231).
    const deadbandInput = screen.getByLabelText(/Deadband \(°C\)/i);
    fireEvent.change(deadbandInput, { target: { value: "1" } });
    // Fail where the damage happens, not later as a confusing POST body (#597).
    expect(deadbandInput).toHaveDisplayValue("1");

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
    await cardsSettled();

    const nameInput = await screen.findByLabelText(/Friendly name/i, {
      selector: "#thermo-climate\\.test-name",
    });

    fireEvent.change(screen.getByLabelText(/Min setpoint \(°C\)/i), { target: { value: "16" } });
    fireEvent.change(screen.getByLabelText(/Max setpoint \(°C\)/i), { target: { value: "27" } });
    fireEvent.change(screen.getByLabelText(/Deadband \(°C\)/i), { target: { value: "0.3" } });
    fireEvent.change(screen.getByLabelText(/Overshoot delta \(°C\)/i), {
      target: { value: "0.3" },
    });
    // Fail at the line where the damage happens: a form reset landing inside
    // fireEvent's act() would revert these to the initial derived values, and
    // that used to surface only as a confusing POST body (#597).
    expect(screen.getByLabelText(/Min setpoint \(°C\)/i)).toHaveDisplayValue("16");

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
    await cardsSettled();

    const nameInput = await screen.findByLabelText(/Friendly name/i, {
      selector: "#thermo-climate\\.test-name",
    });

    fireEvent.change(screen.getByLabelText(/Cooling.*outdoor threshold/i), {
      target: { value: "30" },
    });
    fireEvent.change(screen.getByLabelText(/Heating.*max drift/i), {
      target: { value: "2" },
    });
    // Fail where the damage happens, not later as a confusing POST body (#597).
    expect(screen.getByLabelText(/Cooling.*outdoor threshold/i)).toHaveDisplayValue("30");
    expect(screen.getByLabelText(/Heating.*max drift/i)).toHaveDisplayValue("2");

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
    vi.mocked(api.getRooms).mockResolvedValue([]);
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
    // The card renders with an empty input and fills it once getSensorStaleness()
    // resolves, so wait for the value rather than asserting on first paint —
    // same shape as the fallback test below.
    await waitFor(() => expect(input.value).toBe("30"));

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
    // The card renders the input before getSensorStaleness() resolves, so wait
    // for the fetched value to land before typing — otherwise a late resolve
    // overwrites the typed 42, the same shape as #597.
    await waitFor(() => expect(input.value).toBe("30"));
    fireEvent.change(input, { target: { value: "42" } });
    const card = input.closest(".card") as HTMLElement;
    fireEvent.click(within(card).getByRole("button", { name: /^Save$/i }));

    await screen.findByText("nope");
  });
});

describe("Thermostats Page — empty state", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getRooms).mockResolvedValue([]);
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
    vi.mocked(api.getRooms).mockResolvedValue([]);
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

  // ── Eco Suspend (Issue #500) ──────────────────────────────────────────────

  it("opens the Eco Suspend modal from the page-level button when Eco is enabled", async () => {
    vi.mocked(api.getThermostats).mockResolvedValue([
      { ...mockThermostats[0], eco_mode_enabled: true },
    ]);
    render(<Thermostats />);
    fireEvent.click(await screen.findByTestId("thermostats-eco-suspend-btn"));
    expect(screen.getByText("Suspend Eco Mode")).toBeInTheDocument();
    expect(screen.getByLabelText("Thermostat")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByText("Suspend Eco Mode")).not.toBeInTheDocument();
  });

  it("card header shows a pre-scoped suspend control when Eco is enabled", async () => {
    vi.mocked(api.getThermostats).mockResolvedValue([
      {
        ...mockThermostats[0],
        eco_mode_enabled: true,
        eco_suspend_until: "2099-12-25T10:00:00+00:00",
      },
    ]);
    render(<Thermostats />);
    const cardBtn = await screen.findByText(/Eco suspended until/);
    fireEvent.click(cardBtn);
    expect(screen.getByRole("button", { name: /Resume Eco now/i })).toBeInTheDocument();
    expect(screen.getByLabelText("Thermostat")).toHaveValue(
      mockThermostats[0].thermostat_entity_id
    );
  });

  it("hides every suspend control when Eco is off everywhere (#500 visibility rule)", async () => {
    // Default fixtures: thermostat Eco off, no rooms, no suspension → neither
    // the page-level button nor any card control renders.
    render(<Thermostats />);
    await screen.findByText(mockThermostats[0].name);
    expect(screen.queryByTestId("thermostats-eco-suspend-btn")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Suspend Eco/ })).not.toBeInTheDocument();
  });

  it("shows the suspend controls when only a room opts into Eco", async () => {
    // Thermostat Eco OFF, but a room under it carries an explicit opt-in —
    // Eco is in play for that zone, so both controls must surface.
    vi.mocked(api.getRooms).mockResolvedValue([
      {
        id: "room-1",
        name: "Bedroom",
        thermostat_entity_id: "climate.test",
        include_thermostat_sensor: false,
        presence_holdover_hours: 2,
        temp_offset: 0,
        deadband_override: null,
        notes: "",
        system_wide_temp: null,
        ambient_suppression_enabled: false,
        ambient_suppression_mode: "any_presence",
        ambient_suppression_min_differential: 5,
        ambient_suppression_deadband: 2,
        ambient_suppression_off_schedule_window_min: 60,
        ...ecoRoomDefaults,
        eco_mode_enabled: true,
      },
    ]);
    render(<Thermostats />);
    expect(await screen.findByTestId("thermostats-eco-suspend-btn")).toBeInTheDocument();
    // The card control shows too (the room opt-in puts the zone in play).
    expect(screen.getAllByRole("button", { name: /Suspend Eco/ })).toHaveLength(2);
  });
});
