import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import HoldModal from "./HoldModal";
import * as api from "../api";
import { UnitContext, buildUnitContext } from "../contexts";
import { ecoRoomDefaults, makeHold } from "../testFixtures";

vi.mock("../api");

function room(over: Partial<api.Room> = {}): api.Room {
  return {
    id: "room-1",
    name: "Living Room",
    thermostat_entity_id: "climate.test",
    include_thermostat_sensor: false,
    system_wide_temp: null,
    presence_holdover_hours: 2,
    notes: "",
    temp_offset: 0,
    deadband_override: null,
    ambient_suppression_enabled: false,
    ambient_suppression_mode: "any_presence",
    ambient_suppression_min_differential: 5,
    ambient_suppression_deadband: 2,
    ambient_suppression_off_schedule_window_min: 60,
    ...ecoRoomDefaults,
    ...over,
  };
}

const ROOMS = [room(), room({ id: "room-2", name: "Bedroom" })];

describe("HoldModal (Issue #576)", () => {
  beforeEach(() => vi.clearAllMocks());

  it("defaults the picker to the first room and lets the user choose", () => {
    render(<HoldModal rooms={ROOMS} holds={{}} onClose={() => {}} onChanged={() => {}} />);
    const picker = screen.getByLabelText("Room");
    expect(picker).toHaveValue("room-1");
    fireEvent.change(picker, { target: { value: "room-2" } });
    expect(picker).toHaveValue("room-2");
  });

  it("pre-selects initialRoom when opened from a scoped control", () => {
    render(
      <HoldModal
        rooms={ROOMS}
        initialRoom="room-2"
        holds={{}}
        onClose={() => {}}
        onChanged={() => {}}
      />
    );
    expect(screen.getByLabelText("Room")).toHaveValue("room-2");
  });

  it("marks a held room's option so the picker says which rooms carry a hold", () => {
    render(
      <HoldModal
        rooms={ROOMS}
        holds={{ "room-2": makeHold({ room_id: "room-2" }) }}
        onClose={() => {}}
        onChanged={() => {}}
      />
    );
    expect(screen.getByRole("option", { name: "Bedroom — hold active" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Living Room" })).toBeInTheDocument();
  });

  it("sets a hold with the defaults: 72°F for 2 hours, Eco opted out", async () => {
    vi.mocked(api.setOverride).mockResolvedValue(makeHold());
    const onChanged = vi.fn();
    const onClose = vi.fn();
    render(<HoldModal rooms={ROOMS} holds={{}} onClose={onClose} onChanged={onChanged} />);
    fireEvent.click(screen.getByTestId("hold-modal-save"));
    await waitFor(() => expect(api.setOverride).toHaveBeenCalledWith("room-1", 72, 2, false));
    expect(onChanged).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it("sends the picked duration preset", async () => {
    vi.mocked(api.setOverride).mockResolvedValue(makeHold());
    render(<HoldModal rooms={ROOMS} holds={{}} onClose={() => {}} onChanged={() => {}} />);
    fireEvent.change(screen.getByLabelText(/Hold for/i), { target: { value: "4" } });
    fireEvent.click(screen.getByTestId("hold-modal-save"));
    await waitFor(() => expect(api.setOverride).toHaveBeenCalledWith("room-1", 72, 4, false));
  });

  it("sends respect_eco:true when the Eco checkbox is ticked", async () => {
    vi.mocked(api.setOverride).mockResolvedValue(makeHold({ respect_eco: true }));
    render(<HoldModal rooms={ROOMS} holds={{}} onClose={() => {}} onChanged={() => {}} />);
    fireEvent.click(screen.getByLabelText(/Allow Eco Mode to relax this hold/i));
    fireEvent.click(screen.getByTestId("hold-modal-save"));
    await waitFor(() => expect(api.setOverride).toHaveBeenCalledWith("room-1", 72, 2, true));
  });

  it("rejects an out-of-bounds temperature without calling the API", async () => {
    render(<HoldModal rooms={ROOMS} holds={{}} onClose={() => {}} onChanged={() => {}} />);
    fireEvent.change(screen.getByLabelText(/Hold temperature/i), { target: { value: "95" } });
    fireEvent.click(screen.getByTestId("hold-modal-save"));
    expect(
      await screen.findByText("Hold temperature must be between 40.0°F and 90.0°F")
    ).toBeInTheDocument();
    expect(api.setOverride).not.toHaveBeenCalled();
  });

  it("rejects a non-numeric temperature without calling the API", async () => {
    render(<HoldModal rooms={ROOMS} holds={{}} onClose={() => {}} onChanged={() => {}} />);
    fireEvent.change(screen.getByLabelText(/Hold temperature/i), { target: { value: "" } });
    fireEvent.click(screen.getByTestId("hold-modal-save"));
    expect(await screen.findByText(/Hold temperature must be between/i)).toBeInTheDocument();
    expect(api.setOverride).not.toHaveBeenCalled();
  });

  it("requires a room selection when none are available", async () => {
    render(<HoldModal rooms={[]} holds={{}} onClose={() => {}} onChanged={() => {}} />);
    fireEvent.click(screen.getByTestId("hold-modal-save"));
    expect(await screen.findByText(/choose a room/i)).toBeInTheDocument();
    expect(api.setOverride).not.toHaveBeenCalled();
  });

  it("shows the active hold with Replace + Cancel actions for a held room", async () => {
    vi.mocked(api.clearOverride).mockResolvedValue({});
    const onChanged = vi.fn();
    const onClose = vi.fn();
    render(
      <HoldModal
        rooms={ROOMS}
        initialRoom="room-1"
        holds={{ "room-1": makeHold() }}
        onClose={onClose}
        onChanged={onChanged}
      />
    );
    expect(screen.getByText("Temporary hold active")).toBeInTheDocument();
    // The info line names the current target and countdown (the countdown is a
    // bare text node inside the paragraph — match the surrounding text).
    expect(screen.getByText("75.0°F")).toBeInTheDocument();
    expect(screen.getByText(/ends in 1h 30m/)).toBeInTheDocument();
    // The temp field initialises from the existing hold, not the 72°F default.
    expect(screen.getByLabelText(/Hold temperature/i)).toHaveValue(75);
    // Save is relabelled as a replace.
    expect(screen.getByTestId("hold-modal-save")).toHaveTextContent("Replace hold");

    fireEvent.click(screen.getByTestId("hold-modal-cancel-hold"));
    await waitFor(() => expect(api.clearOverride).toHaveBeenCalledWith("room-1"));
    expect(onChanged).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it("hides the Cancel-hold button when the selected room has no hold", () => {
    render(<HoldModal rooms={ROOMS} holds={{}} onClose={() => {}} onChanged={() => {}} />);
    expect(screen.queryByTestId("hold-modal-cancel-hold")).not.toBeInTheDocument();
    expect(screen.getByTestId("hold-modal-save")).toHaveTextContent("Set hold");
  });

  it("loads a held room's target and Eco opt-in when it is picked", () => {
    render(
      <HoldModal
        rooms={ROOMS}
        holds={{ "room-2": makeHold({ room_id: "room-2", target_temp: 68, respect_eco: true }) }}
        onClose={() => {}}
        onChanged={() => {}}
      />
    );
    // room-1 (no hold) is selected: form shows the defaults.
    expect(screen.getByLabelText(/Hold temperature/i)).toHaveValue(72);
    expect(screen.getByLabelText(/Allow Eco Mode/i)).not.toBeChecked();

    // Switching to the held room pulls in ITS hold.
    fireEvent.change(screen.getByLabelText("Room"), { target: { value: "room-2" } });
    expect(screen.getByLabelText(/Hold temperature/i)).toHaveValue(68);
    expect(screen.getByLabelText(/Allow Eco Mode/i)).toBeChecked();
    expect(screen.getByTestId("hold-modal-save")).toHaveTextContent("Replace hold");
  });

  it("closes on backdrop click without saving", () => {
    const onClose = vi.fn();
    const { container } = render(
      <HoldModal rooms={ROOMS} holds={{}} onClose={onClose} onChanged={() => {}} />
    );
    fireEvent.click(container.querySelector(".modal-backdrop") as HTMLElement);
    expect(onClose).toHaveBeenCalled();
    expect(api.setOverride).not.toHaveBeenCalled();
  });

  it("surfaces a save failure as an inline error and stays open", async () => {
    vi.mocked(api.setOverride).mockRejectedValue(
      new Error("duration_hours must be greater than 0")
    );
    const onClose = vi.fn();
    render(<HoldModal rooms={ROOMS} holds={{}} onClose={onClose} onChanged={() => {}} />);
    fireEvent.click(screen.getByTestId("hold-modal-save"));
    expect(await screen.findByText(/duration_hours must be greater than 0/i)).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByTestId("hold-modal")).toBeInTheDocument();
  });

  it("surfaces a cancel-hold failure as an inline error and stays open", async () => {
    vi.mocked(api.clearOverride).mockRejectedValue(new Error("boom"));
    const onClose = vi.fn();
    render(
      <HoldModal
        rooms={ROOMS}
        holds={{ "room-1": makeHold() }}
        onClose={onClose}
        onChanged={() => {}}
      />
    );
    fireEvent.click(screen.getByTestId("hold-modal-cancel-hold"));
    expect(await screen.findByText("boom")).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("HoldModal — Celsius mode (#231)", () => {
  const renderInCelsius = (holds: Record<string, api.RoomOverrideHold> = {}) =>
    render(
      <UnitContext.Provider value={buildUnitContext("C")}>
        <HoldModal rooms={ROOMS} holds={holds} onClose={() => {}} onChanged={() => {}} />
      </UnitContext.Provider>
    );

  beforeEach(() => vi.clearAllMocks());

  it("initialises the default target in °C", () => {
    renderInCelsius();
    // 72°F default → toDisplay(72) = 22.2°C.
    expect(screen.getByLabelText(/Hold temperature \(°C\)/i)).toHaveValue(22.2);
  });

  it("sends the user's raw °C target, not a pre-converted °F value (#231)", async () => {
    vi.mocked(api.setOverride).mockResolvedValue(makeHold());
    renderInCelsius();
    fireEvent.change(screen.getByLabelText(/Hold temperature/i), { target: { value: "22" } });
    fireEvent.click(screen.getByTestId("hold-modal-save"));
    // 22, NOT 71.6 (toStorage) — the backend's _to_f converts at the write
    // boundary; converting here would re-introduce the #231 double-conversion.
    await waitFor(() => expect(api.setOverride).toHaveBeenCalledWith("room-1", 22, 2, false));
  });

  it("advertises °C bounds the backend accepts and rejects values outside them (#521)", async () => {
    renderInCelsius();
    // displayBound, not raw toDisplay: toDisplay(40) = 4.4°C converts back to
    // 39.92°F and the backend refuses the advertised minimum.
    const input = screen.getByLabelText(/Hold temperature/i);
    expect(input).toHaveAttribute("min", "4.5");
    expect(input).toHaveAttribute("max", "32.2");

    fireEvent.change(input, { target: { value: "4.4" } });
    fireEvent.click(screen.getByTestId("hold-modal-save"));
    expect(
      await screen.findByText("Hold temperature must be between 4.5°C and 32.2°C")
    ).toBeInTheDocument();
    expect(api.setOverride).not.toHaveBeenCalled();
  });

  it("shows an existing hold's target in °C", () => {
    renderInCelsius({ "room-1": makeHold() });
    // Stored 75°F → fmtTemp = "23.9°C"; the editable field holds toDisplay(75).
    expect(screen.getByText("23.9°C")).toBeInTheDocument();
    expect(screen.getByLabelText(/Hold temperature/i)).toHaveValue(23.9);
  });
});
