import { StrictMode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import * as api from "../api";
import { ecoRoomDefaults, ecoThermostatDefaults, makeHold } from "../testFixtures";
import { AuthContext, McpContext, UnitContext, buildUnitContext } from "../contexts";

import AirflowConfigBanner from "./AirflowConfigBanner";
import ConfirmDialog from "./ConfirmDialog";
import EcoSuspendBanner from "./EcoSuspendBanner";
import EcoSuspendModal from "./EcoSuspendModal";
import HoldModal from "./HoldModal";
import McpServerCard from "./McpServerCard";
import McpTokensCard from "./McpTokensCard";
import OutsideTempPicker from "./OutsideTempPicker";
import UnitChangeBanner from "./UnitChangeBanner";
import VacationModeBanner from "./VacationModeBanner";
import StaleSensorsBanner from "./StaleSensorsBanner";
import UnavailableThermostatsBanner from "./UnavailableThermostatsBanner";
import VacationModeModal from "./VacationModeModal";

vi.mock("../api");

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

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
    ...over,
  };
}

function room(over: Partial<api.Room> = {}): api.Room {
  return {
    id: "room-1",
    name: "Living Room",
    thermostat_entity_id: "climate.up",
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

function settings(over: Partial<api.AppSettings> = {}): api.AppSettings {
  return {
    temperature_unit: "F",
    theme: "system",
    unit_change_ack_required: false,
    vacation_mode: { enabled: false, return_at: null },
    eco_suspend: {},
    ...over,
  };
}

// A rejection that is NOT an Error instance — the `e instanceof Error` guards
// in every save handler fall through to their hardcoded fallback copy for
// these. `fetch` rejecting with a DOMException / a thrown string both land
// here in production.
const NOT_AN_ERROR = { status: 500, detail: "not an Error instance" };

// ---------------------------------------------------------------------------

describe("AirflowConfigBanner — unnamed thermostats fall back to the entity id", () => {
  beforeEach(() => vi.clearAllMocks());

  it("names a single unconfigured thermostat by entity id when it has no name", async () => {
    vi.mocked(api.getThermostats).mockResolvedValue([
      tc({ thermostat_entity_id: "climate.solo", name: "", total_vents_count: null }),
    ]);
    render(<AirflowConfigBanner />);
    const banner = await screen.findByTestId("airflow-config-banner");
    expect(banner).toHaveTextContent("climate.solo");
    expect(banner).toHaveTextContent(/transitional default/);
  });

  it("names each unnamed thermostat by entity id in the multi-thermostat list", async () => {
    vi.mocked(api.getThermostats).mockResolvedValue([
      tc({ thermostat_entity_id: "climate.one", name: "", total_vents_count: null }),
      tc({ thermostat_entity_id: "climate.two", name: "", total_vents_count: null }),
    ]);
    render(<AirflowConfigBanner />);
    const banner = await screen.findByTestId("airflow-config-banner");
    expect(banner).toHaveTextContent(/2 thermostats/);
    const items = banner.querySelectorAll("li");
    expect(Array.from(items).map((li) => li.textContent)).toEqual(["climate.one", "climate.two"]);
  });
});

describe("ConfirmDialog", () => {
  it("cancels when the backdrop itself is clicked", () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    render(
      <ConfirmDialog
        title="Delete room?"
        message="This cannot be undone."
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    fireEvent.click(screen.getByTestId("confirm-dialog"));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("does NOT cancel when the click lands inside the dialog body", () => {
    const onCancel = vi.fn();
    render(
      <ConfirmDialog
        title="Delete room?"
        message="This cannot be undone."
        onConfirm={vi.fn()}
        onCancel={onCancel}
      />
    );
    // The click bubbles up to the backdrop, but e.target is the inner panel —
    // a mis-written handler that omitted the target check would close here and
    // lose the user's place on every stray click inside the dialog.
    fireEvent.click(screen.getByText("This cannot be undone."));
    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.getByTestId("confirm-dialog")).toBeInTheDocument();
  });

  it("uses the default button labels and portals to document.body", () => {
    const { container } = render(
      <ConfirmDialog title="T" message="M" onConfirm={vi.fn()} onCancel={vi.fn()} />
    );
    // createPortal: nothing lands in the render container itself.
    expect(container).toBeEmptyDOMElement();
    expect(screen.getByRole("button", { name: "Confirm" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
  });
});

describe("EcoSuspendBanner — unnamed thermostat", () => {
  beforeEach(() => vi.clearAllMocks());

  it("falls back to the entity id when the thermostat has no name", async () => {
    vi.mocked(api.getThermostats).mockResolvedValue([
      tc({
        thermostat_entity_id: "climate.nameless",
        name: "",
        eco_suspend_until: "2099-12-25T10:00:00+00:00",
      }),
    ]);
    render(<EcoSuspendBanner />);
    const banner = await screen.findByTestId("eco-suspend-banner");
    expect(banner).toHaveTextContent("climate.nameless");
    expect(screen.getByText("climate.nameless").tagName).toBe("STRONG");
  });
});

describe("EcoSuspendModal — fallbacks and non-Error failures", () => {
  beforeEach(() => vi.clearAllMocks());

  it("labels an unnamed thermostat by its entity id in the picker", () => {
    render(
      <EcoSuspendModal
        thermostats={[tc({ thermostat_entity_id: "climate.nameless", name: "" })]}
        onClose={() => {}}
        onChanged={() => {}}
      />
    );
    expect(screen.getByRole("option", { name: /climate\.nameless/ })).toBeInTheDocument();
  });

  it("echoes an unparseable eco_suspend_until verbatim instead of 'Invalid Date'", () => {
    render(
      <EcoSuspendModal
        thermostats={[tc({ eco_suspend_until: "whenever" })]}
        initialThermostat="climate.up"
        onClose={() => {}}
        onChanged={() => {}}
      />
    );
    expect(screen.getByText("whenever")).toBeInTheDocument();
    expect(screen.queryByText(/Invalid Date/)).toBeNull();
  });

  it("falls back to generic copy when suspending rejects with a non-Error", async () => {
    vi.mocked(api.setEcoSuspend).mockRejectedValue(NOT_AN_ERROR);
    const onClose = vi.fn();
    render(<EcoSuspendModal thermostats={[tc()]} onClose={onClose} onChanged={() => {}} />);
    fireEvent.change(screen.getByLabelText(/Resume Eco at/i), {
      target: { value: "2099-06-01T18:00" },
    });
    fireEvent.click(screen.getByText("Suspend Eco"));
    expect(await screen.findByText("Failed to suspend Eco Mode")).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("falls back to generic copy when resume-now rejects with a non-Error", async () => {
    vi.mocked(api.clearEcoSuspend).mockRejectedValue(NOT_AN_ERROR);
    const onClose = vi.fn();
    render(
      <EcoSuspendModal
        thermostats={[tc({ eco_suspend_until: "2099-12-25T10:00:00+00:00" })]}
        onClose={onClose}
        onChanged={() => {}}
      />
    );
    fireEvent.click(screen.getByRole("button", { name: /Resume Eco now/i }));
    expect(await screen.findByText("Failed to resume Eco Mode")).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("HoldModal — non-Error failures", () => {
  const ROOMS = [room()];
  beforeEach(() => vi.clearAllMocks());

  it("falls back to generic copy when the save rejects with a non-Error", async () => {
    vi.mocked(api.setOverride).mockRejectedValue(NOT_AN_ERROR);
    const onClose = vi.fn();
    render(<HoldModal rooms={ROOMS} holds={{}} onClose={onClose} onChanged={() => {}} />);
    fireEvent.click(screen.getByTestId("hold-modal-save"));
    expect(await screen.findByText("Failed to set hold")).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("falls back to generic copy when cancelling rejects with a non-Error", async () => {
    vi.mocked(api.clearOverride).mockRejectedValue(NOT_AN_ERROR);
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
    expect(await screen.findByText("Failed to cancel hold")).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("McpServerCard", () => {
  const renderCard = (mcpEnabled: boolean, requireAuth = false, toggleMcp = vi.fn()) =>
    render(
      <AuthContext.Provider value={{ requireAuth, method: "open", logout: async () => {} }}>
        <McpContext.Provider value={{ mcpEnabled, toggleMcp }}>
          <McpServerCard />
        </McpContext.Provider>
      </AuthContext.Provider>
    );

  it("closes the confirm modal when the backdrop itself is clicked", () => {
    const toggleMcp = vi.fn().mockResolvedValue(undefined);
    const { container } = renderCard(false, false, toggleMcp);
    fireEvent.click(screen.getByRole("button", { name: "Turn on" }));
    expect(screen.getByText("Turn on MCP server?")).toBeInTheDocument();

    fireEvent.click(container.querySelector(".modal-backdrop") as HTMLElement);
    expect(screen.queryByText("Turn on MCP server?")).toBeNull();
    expect(toggleMcp).not.toHaveBeenCalled();
  });

  it("keeps the confirm modal open when the click lands inside it", () => {
    const { container } = renderCard(true);
    fireEvent.click(screen.getByRole("button", { name: "Turn off" }));
    fireEvent.click(container.querySelector(".modal-title") as HTMLElement);
    expect(screen.getByText("Turn off MCP server?")).toBeInTheDocument();
  });

  it("toggles the server and closes the modal on Confirm", async () => {
    const toggleMcp = vi.fn().mockResolvedValue(undefined);
    renderCard(false, true, toggleMcp);
    fireEvent.click(screen.getByRole("button", { name: "Turn on" }));
    // requireAuth on: the confirm modal says to mint a token afterwards.
    expect(screen.getByText(/mint a bearer token after enabling/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(toggleMcp).toHaveBeenCalledTimes(1));
    expect(screen.queryByText("Turn on MCP server?")).toBeNull();
  });
});

describe("McpTokensCard — load failure, guards and fallbacks", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listMcpTokens).mockResolvedValue([]);
  });

  it("degrades to an empty list when the token fetch fails", async () => {
    vi.mocked(api.listMcpTokens).mockRejectedValue(new Error("401"));
    render(<McpTokensCard />);
    await waitFor(() => expect(api.listMcpTokens).toHaveBeenCalled());
    expect(await screen.findByText("No tokens yet.")).toBeInTheDocument();
  });

  it("shows a token's last_used_at timestamp when it has been used", async () => {
    vi.mocked(api.listMcpTokens).mockResolvedValue([
      {
        id: "t1",
        label: "Claude",
        scope: "read",
        created_at: "2025-01-01",
        last_used_at: "2025-02-03T04:05:06Z",
      },
    ]);
    render(<McpTokensCard />);
    expect(await screen.findByText(/Last used: 2025-02-03T04:05:06Z/)).toBeInTheDocument();
  });

  it("refuses to mint when the form is submitted with a blank label", async () => {
    const { container } = render(<McpTokensCard />);
    await waitFor(() => expect(api.listMcpTokens).toHaveBeenCalled());
    // The button is disabled, but Enter in the label field still submits the
    // form — the handler's own guard is what stops a blank-label POST.
    fireEvent.change(screen.getByLabelText(/Label/i), { target: { value: "   " } });
    fireEvent.submit(container.querySelector("form.mcp-token-form") as HTMLFormElement);
    await waitFor(() => expect(api.listMcpTokens).toHaveBeenCalledTimes(1));
    expect(api.mintMcpToken).not.toHaveBeenCalled();
  });

  it("falls back to generic copy when minting rejects with a non-Error", async () => {
    vi.mocked(api.mintMcpToken).mockRejectedValue(NOT_AN_ERROR);
    render(<McpTokensCard />);
    fireEvent.change(await screen.findByLabelText(/Label/i), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: /Mint token/i }));
    expect(await screen.findByText("Failed to mint token")).toBeInTheDocument();
  });

  it("falls back to generic copy when a scope update rejects with a non-Error", async () => {
    vi.mocked(api.listMcpTokens).mockResolvedValue([
      { id: "t1", label: "Old", scope: "read", created_at: "x", last_used_at: null },
    ]);
    vi.mocked(api.updateMcpTokenScope).mockRejectedValue(NOT_AN_ERROR);
    render(<McpTokensCard />);
    fireEvent.click(await screen.findByRole("button", { name: /Edit/i }));
    fireEvent.click(screen.getByRole("button", { name: /Save/i }));
    expect(await screen.findByText("Failed to update scope")).toBeInTheDocument();
    // The row stays in edit mode so the user can retry.
    expect(screen.getByLabelText(/Scope for Old/i)).toBeInTheDocument();
  });
});

describe("OutsideTempPicker — non-Error failures", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getHAEntities).mockResolvedValue([
      { entity_id: "sensor.outdoor", state: "50", friendly_name: "Outdoor Sensor" },
    ]);
  });

  const renderPicker = () =>
    render(
      <UnitContext.Provider value={buildUnitContext("F")}>
        <OutsideTempPicker />
      </UnitContext.Provider>
    );

  it("falls back to generic copy when the load rejects with a non-Error", async () => {
    vi.mocked(api.getOutsideTempEntity).mockRejectedValue(NOT_AN_ERROR);
    renderPicker();
    expect(await screen.findByText("Failed to load")).toBeInTheDocument();
  });

  it("falls back to generic copy when the save rejects with a non-Error", async () => {
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: "sensor.outdoor",
      current_value: 50,
    });
    vi.mocked(api.setOutsideTempEntity).mockRejectedValue(NOT_AN_ERROR);
    renderPicker();
    fireEvent.click(await screen.findByRole("button", { name: "Clear" }));
    expect(await screen.findByText("Save failed")).toBeInTheDocument();
    // The previously configured entity is still shown — a failed save must not
    // look like a successful clear.
    expect(screen.getByText(/sensor\.outdoor/)).toBeInTheDocument();
  });
});

describe("UnitChangeBanner — re-entrancy guards", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getSettings).mockResolvedValue(
      settings({ temperature_unit: "C", unit_change_ack_required: true })
    );
  });

  it("issues at most one restart while one is already in flight", async () => {
    // A restart never resolves (the server is going down), so `restarting`
    // stays true and a second activation must be a no-op.
    vi.mocked(api.restartApp).mockReturnValue(new Promise(() => {}));
    render(<UnitChangeBanner />);
    const button = await screen.findByText("Restart Plenum");
    fireEvent.click(button);
    await waitFor(() => expect(screen.getByText("Restarting…")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Restarting…"));
    fireEvent.click(screen.getByText("Restarting…"));
    expect(api.restartApp).toHaveBeenCalledTimes(1);
  });

  it("issues at most one ack while one is already in flight", async () => {
    vi.mocked(api.ackUnitChange).mockReturnValue(new Promise(() => {}));
    render(<UnitChangeBanner />);
    fireEvent.click(await screen.findByText("I've reviewed my settings"));
    await waitFor(() => expect(screen.getByText("…")).toBeInTheDocument());
    fireEvent.click(screen.getByText("…"));
    fireEvent.click(screen.getByText("…"));
    expect(api.ackUnitChange).toHaveBeenCalledTimes(1);
  });
});

describe("VacationModeBanner — no return date / fetch failure", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders nothing when the settings fetch fails", async () => {
    vi.mocked(api.getSettings).mockRejectedValue(new Error("network blip"));
    const { container } = render(<VacationModeBanner />);
    await waitFor(() => expect(api.getSettings).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("renders an empty return date rather than 'Invalid Date' when return_at is null", async () => {
    vi.mocked(api.getSettings).mockResolvedValue(
      settings({ vacation_mode: { enabled: true, return_at: null } })
    );
    render(<VacationModeBanner />);
    const banner = await screen.findByTestId("vacation-mode-banner");
    expect(banner.textContent).toContain("Returning .");
    expect(banner.textContent).not.toContain("Invalid Date");
  });
});

describe("VacationModeModal — backdrop, date fallbacks and failures", () => {
  const OFF: api.VacationMode = { enabled: false, return_at: null };
  const ON: api.VacationMode = { enabled: true, return_at: "2026-12-25T10:00:00.000Z" };
  const future = () => new Date(Date.now() + 86_400_000).toISOString().slice(0, 16);

  beforeEach(() => vi.clearAllMocks());

  it("closes on a backdrop click and stays open on a click inside the panel", () => {
    const onClose = vi.fn();
    const { container } = render(
      <VacationModeModal current={OFF} onClose={onClose} onChanged={vi.fn()} />
    );
    fireEvent.click(container.querySelector(".modal") as HTMLElement);
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(container.querySelector(".modal-backdrop") as HTMLElement);
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(api.enableVacationMode).not.toHaveBeenCalled();
  });

  it("renders an empty return date when an active vacation has no return_at", () => {
    const { container } = render(
      <VacationModeModal
        current={{ enabled: true, return_at: null }}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />
    );
    expect(container.querySelector(".modal")?.textContent).toContain("active until .");
    expect(screen.queryByText(/Invalid Date/)).toBeNull();
  });

  it("echoes an unparseable return_at verbatim instead of 'Invalid Date'", () => {
    render(
      <VacationModeModal
        current={{ enabled: true, return_at: "sometime-next-week" }}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />
    );
    expect(screen.getByText("sometime-next-week")).toBeInTheDocument();
    expect(screen.queryByText(/Invalid Date/)).toBeNull();
  });

  it("surfaces an enable failure's message inline and keeps the modal open", async () => {
    vi.mocked(api.enableVacationMode).mockRejectedValue(new Error("return_at is in the past"));
    const onClose = vi.fn();
    const onChanged = vi.fn();
    render(<VacationModeModal current={OFF} onClose={onClose} onChanged={onChanged} />);
    fireEvent.change(screen.getByLabelText(/Return date/i), { target: { value: future() } });
    fireEvent.click(
      screen.getAllByText(/Enable vacation mode/i).find((el) => el.tagName === "BUTTON")!
    );
    expect(await screen.findByText("return_at is in the past")).toBeInTheDocument();
    expect(onChanged).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    // The button returns to its idle label so the user can retry.
    await waitFor(() =>
      expect(
        screen.getAllByText(/Enable vacation mode/i).find((el) => el.tagName === "BUTTON")
      ).toBeEnabled()
    );
  });

  it("falls back to generic copy when enabling rejects with a non-Error", async () => {
    vi.mocked(api.enableVacationMode).mockRejectedValue(NOT_AN_ERROR);
    render(<VacationModeModal current={OFF} onClose={vi.fn()} onChanged={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(/Return date/i), { target: { value: future() } });
    fireEvent.click(
      screen.getAllByText(/Enable vacation mode/i).find((el) => el.tagName === "BUTTON")!
    );
    expect(await screen.findByText("Failed to enable vacation mode")).toBeInTheDocument();
  });

  it("surfaces a disable failure inside the confirmation step", async () => {
    vi.mocked(api.disableVacationMode).mockRejectedValue(new Error("vacation mode is not active"));
    const onClose = vi.fn();
    render(<VacationModeModal current={ON} onClose={onClose} onChanged={vi.fn()} />);
    fireEvent.click(screen.getByText(/End vacation mode early/i));
    fireEvent.click(screen.getByText(/Yes, end vacation mode/i));
    expect(await screen.findByText("vacation mode is not active")).toBeInTheDocument();
    // Still on the confirmation step, not closed and not reverted.
    expect(screen.getByText(/End vacation mode\?/i)).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("falls back to generic copy when disabling rejects with a non-Error", async () => {
    vi.mocked(api.disableVacationMode).mockRejectedValue(NOT_AN_ERROR);
    render(<VacationModeModal current={ON} onClose={vi.fn()} onChanged={vi.fn()} />);
    fireEvent.click(screen.getByText(/End vacation mode early/i));
    fireEvent.click(screen.getByText(/Yes, end vacation mode/i));
    expect(await screen.findByText("Failed to disable vacation mode")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Mount-fetch cancellation guards.
//
// StrictMode double-invokes effects, so the FIRST effect's fetch is already in
// flight when its cleanup runs. Without the `cancelled` flag that stale
// response lands after the second, live one and overwrites it with data the
// component has already superseded. Resolving the two fetches out of order is
// the only way to observe the difference.
// ---------------------------------------------------------------------------

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

describe("mount-fetch cancellation guards (StrictMode)", () => {
  beforeEach(() => vi.clearAllMocks());

  it("AirflowConfigBanner ignores the superseded first fetch", async () => {
    const first = deferred<api.ThermostatConfig[]>();
    const second = deferred<api.ThermostatConfig[]>();
    vi.mocked(api.getThermostats)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    render(
      <StrictMode>
        <AirflowConfigBanner />
      </StrictMode>
    );
    await waitFor(() => expect(api.getThermostats).toHaveBeenCalledTimes(2));

    // The live (second) effect resolves first…
    await act(async () => {
      second.resolve([
        tc({ thermostat_entity_id: "climate.live", name: "Live", total_vents_count: null }),
      ]);
    });
    expect(await screen.findByTestId("airflow-config-banner")).toHaveTextContent("Live");

    // …then the abandoned first effect's response arrives and must be dropped.
    await act(async () => {
      first.resolve([
        tc({ thermostat_entity_id: "climate.stale", name: "Stale", total_vents_count: null }),
      ]);
    });
    const banner = screen.getByTestId("airflow-config-banner");
    expect(banner).toHaveTextContent("Live");
    expect(banner).not.toHaveTextContent("Stale");
  });

  it("StaleSensorsBanner ignores the superseded first fetch", async () => {
    const first = deferred<api.SensorHealth>();
    const second = deferred<api.SensorHealth>();
    vi.mocked(api.getSensorHealth)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    render(
      <StrictMode>
        <StaleSensorsBanner />
      </StrictMode>
    );
    await waitFor(() => expect(api.getSensorHealth).toHaveBeenCalledTimes(2));

    await act(async () => {
      second.resolve({
        stale_after_min: 90,
        rooms: [
          {
            room_id: "room-live",
            room_name: "Live Room",
            thermostat_entity_id: "climate.up",
            stale_sensors: [{ entity_id: "sensor.live", age_seconds: 600, reason: "stale" }],
          },
        ],
      });
    });
    expect(await screen.findByTestId("stale-sensors-banner")).toHaveTextContent("Live Room");

    await act(async () => {
      first.resolve({
        stale_after_min: 90,
        rooms: [
          {
            room_id: "room-stale",
            room_name: "Stale Room",
            thermostat_entity_id: "climate.up",
            stale_sensors: [{ entity_id: "sensor.stale", age_seconds: 600, reason: "stale" }],
          },
        ],
      });
    });
    const banner = screen.getByTestId("stale-sensors-banner");
    expect(banner).toHaveTextContent("Live Room");
    expect(banner).not.toHaveTextContent("Stale Room");
  });

  it("UnavailableThermostatsBanner ignores the superseded first fetch", async () => {
    const first = deferred<api.ThermostatHealth>();
    const second = deferred<api.ThermostatHealth>();
    vi.mocked(api.getThermostatHealth)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    const unavailable = (over: Partial<api.UnavailableThermostat>): api.UnavailableThermostat => ({
      thermostat_entity_id: "climate.up",
      name: "Upstairs",
      reason: "unavailable",
      unavailable_seconds: 300,
      abort_after_min: 30,
      cycle_running: false,
      ...over,
    });

    render(
      <StrictMode>
        <UnavailableThermostatsBanner />
      </StrictMode>
    );
    await waitFor(() => expect(api.getThermostatHealth).toHaveBeenCalledTimes(2));

    await act(async () => {
      second.resolve({ thermostats: [unavailable({ name: "Live Zone" })] });
    });
    expect(await screen.findByTestId("unavailable-thermostats-banner")).toHaveTextContent(
      "Live Zone"
    );

    await act(async () => {
      first.resolve({ thermostats: [unavailable({ name: "Stale Zone" })] });
    });
    const banner = screen.getByTestId("unavailable-thermostats-banner");
    expect(banner).toHaveTextContent("Live Zone");
    expect(banner).not.toHaveTextContent("Stale Zone");
  });
});
