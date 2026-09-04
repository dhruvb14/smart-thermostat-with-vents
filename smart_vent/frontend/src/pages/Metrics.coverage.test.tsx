/**
 * Coverage companion to Metrics.test.tsx — the failure, empty-state and
 * degraded-payload paths the happy-path suite never reaches.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import Metrics from "./Metrics";
import * as api from "../api";
import { ecoThermostatDefaults } from "../testFixtures";
import { UnitContext, buildUnitContext } from "../contexts";

vi.mock("../api");

// The chart grid is rendered by this page but its internals are covered by
// MetricsCharts tests; a zero-size wrapper keeps recharts quiet here.
vi.mock("recharts", async () => {
  const actual = await vi.importActual<typeof import("recharts")>("recharts");
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
      <div style={{ width: 800, height: 300 }}>{children}</div>
    ),
  };
});

const thermostat = (over: Partial<api.ThermostatConfig> = {}): api.ThermostatConfig => ({
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
  ...over,
});

const baseSummary: api.MetricsSummary = {
  start_date: "2024-01-01",
  end_date: "2024-01-07",
  thermostat_entity_id: null,
  heating_seconds: 3600,
  cooling_seconds: 7200,
  cycle_count: 10,
  completed_count: 8,
  timeout_count: 1,
  aborted_count: 1,
  duty_cycle_pct: 15.5,
  source_breakdown: { schedule: 7 },
  avg_outside_temp_at_start: 45,
  avg_outside_temp_at_end: 46.5,
  avg_cycle_duration_seconds: 1800,
  thermostat_count: 1,
  eco_cycle_count: 2,
  eco_seconds: 3000,
};

const baseEco: api.EcoImpact = {
  start_date: "2024-01-01",
  end_date: "2024-01-07",
  thermostat_entity_id: null,
  total_cycles: 10,
  total_seconds: 10800,
  eco_active_cycles: 2,
  eco_active_seconds: 3000,
  avg_drift_f: 2.5,
  days: [],
  rooms: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getThermostats).mockResolvedValue([thermostat()]);
  vi.mocked(api.getMetricsHomeSummary).mockResolvedValue(baseSummary);
  vi.mocked(api.getMetricsThermostatSummary).mockResolvedValue({
    ...baseSummary,
    thermostat_entity_id: "climate.test",
  });
  vi.mocked(api.getMetricsEcoImpact).mockResolvedValue(baseEco);
  vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
    entity_id: "sensor.outside_temp",
    current_value: 42,
  });
  // Selecting a thermostat mounts the per-thermostat chart grid; every one of
  // those endpoints must resolve or the charts throw on `undefined.then`.
  vi.mocked(api.getMetricsTimeseries).mockResolvedValue({
    thermostat_entity_id: "climate.test",
    metric: "hours",
    granularity: "day",
    start: "",
    end: "",
    series: [],
  });
  vi.mocked(api.getMetricsCyclesVsOutsideTemp).mockResolvedValue({
    thermostat_entity_id: "climate.test",
    start: "",
    end: "",
    points: [],
  });
  vi.mocked(api.getMetricsRoomBreakdown).mockResolvedValue({
    thermostat_entity_id: "climate.test",
    start: "",
    end: "",
    rooms: [],
  });
  vi.mocked(api.getMetricsOvershootHistogram).mockResolvedValue({
    thermostat_entity_id: "climate.test",
    start_date: "",
    end_date: "",
    bin_size: 1,
    labels: [],
    counts: [],
    total_room_cycles: 0,
    overshot_count: 0,
    overshot_pct: 0,
    max_overshoot_f: 0,
    avg_overshoot_f: 0,
  });
  vi.mocked(api.getMetricsHourHeatmap).mockResolvedValue({
    thermostat_entity_id: "climate.test",
    start_date: "",
    end_date: "",
    day_labels: [],
    grid_seconds: [],
  });
  vi.mocked(api.getMetricsVentTimeline).mockResolvedValue({
    thermostat_entity_id: "climate.test",
    start: "",
    end: "",
    note: "",
    events: [],
  });
});

// ---------------------------------------------------------------------------
// Summary tiles
// ---------------------------------------------------------------------------

describe("SummarySection", () => {
  it("renders 0m — not an empty tile — when a total is zero", async () => {
    vi.mocked(api.getMetricsHomeSummary).mockResolvedValue({
      ...baseSummary,
      heating_seconds: 0,
      cooling_seconds: 0,
    });
    render(<Metrics />);
    await screen.findByText("Heating time");
    expect(screen.getAllByText("0m")).toHaveLength(2);
  });

  it("annotates the aggregate heating tile with the thermostat count", async () => {
    vi.mocked(api.getMetricsHomeSummary).mockResolvedValue({
      ...baseSummary,
      thermostat_entity_id: null,
      thermostat_count: 3,
    });
    render(<Metrics />);
    expect(await screen.findByText("3 thermostats")).toBeInTheDocument();
  });

  it("omits the thermostat-count hint when the summary is scoped to one thermostat", async () => {
    vi.mocked(api.getMetricsThermostatSummary).mockResolvedValue({
      ...baseSummary,
      thermostat_entity_id: "climate.test",
      thermostat_count: 3,
    });
    render(<Metrics />);
    const select = await screen.findByDisplayValue(/All thermostats/i);
    fireEvent.change(select, { target: { value: "climate.test" } });
    await waitFor(() => expect(api.getMetricsThermostatSummary).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByText("3 thermostats")).not.toBeInTheDocument());
  });

  it("shows an em-dash when no outside temperature was recorded", async () => {
    vi.mocked(api.getMetricsHomeSummary).mockResolvedValue({
      ...baseSummary,
      avg_outside_temp_at_start: null,
    });
    render(<Metrics />);
    await screen.findByText("Avg outside temp");
    const tile = screen.getByText("Avg outside temp").parentElement!;
    expect(tile).toHaveTextContent("—");
    expect(tile).not.toHaveTextContent("°F");
  });

  it("renders the outside temperature in the active display unit", async () => {
    render(
      <UnitContext.Provider value={buildUnitContext("C")}>
        <Metrics />
      </UnitContext.Provider>
    );
    // 45 °F stored → 7.2 °C displayed (absolute conversion).
    expect(await screen.findByText("7.2°C")).toBeInTheDocument();
  });

  it("renders no tiles at all when the summary never arrives", async () => {
    vi.mocked(api.getMetricsHomeSummary).mockRejectedValue(new Error("summary blew up"));
    render(<Metrics />);
    expect(await screen.findByText("summary blew up")).toBeInTheDocument();
    expect(screen.queryByText("Heating time")).not.toBeInTheDocument();
    expect(screen.queryByText("Duty cycle")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Fetch failures
// ---------------------------------------------------------------------------

describe("failure handling", () => {
  it("shows a generic message when the summary rejects with a non-Error", async () => {
    vi.mocked(api.getMetricsHomeSummary).mockRejectedValue("kaboom");
    render(<Metrics />);
    expect(await screen.findByText("Failed to load metrics")).toBeInTheDocument();
  });

  it("surfaces a thermostat-list failure message", async () => {
    vi.mocked(api.getThermostats).mockRejectedValue(new Error("thermostat list down"));
    render(<Metrics />);
    expect(await screen.findByText("thermostat list down")).toBeInTheDocument();
    // The selector still renders with only the home option.
    expect(screen.getByDisplayValue(/All thermostats/i)).toBeInTheDocument();
  });

  it("shows a generic message when the thermostat-list rejects with a non-Error", async () => {
    vi.mocked(api.getThermostats).mockRejectedValue({ status: 500 });
    render(<Metrics />);
    expect(await screen.findByText("Failed to load thermostats")).toBeInTheDocument();
  });

  it("treats a failed outside-temp lookup as 'not configured'", async () => {
    vi.mocked(api.getOutsideTempEntity).mockRejectedValue(new Error("nope"));
    render(<Metrics />);
    expect(
      await screen.findByText(/Outside-temperature sensor not configured/i)
    ).toBeInTheDocument();
    // The lookup failure itself is not surfaced as a page error — it is not
    // an error condition, just an unconfigured optional sensor.
    expect(screen.queryByText("nope")).not.toBeInTheDocument();
  });
});

describe("unmount mid-flight", () => {
  it("drops an eco-impact response that lands after the page unmounted", async () => {
    let resolve!: (v: api.EcoImpact) => void;
    vi.mocked(api.getMetricsEcoImpact).mockReturnValue(
      new Promise<api.EcoImpact>((r) => {
        resolve = r;
      })
    );
    const { container, unmount } = render(<Metrics />);
    unmount();
    await act(async () => {
      resolve(baseEco);
    });
    expect(container.innerHTML).toBe("");
    expect(screen.queryByText(/Eco Mode impact/i)).not.toBeInTheDocument();
  });

  it("drops an eco-impact failure that lands after the page unmounted", async () => {
    let reject!: (e: unknown) => void;
    vi.mocked(api.getMetricsEcoImpact).mockReturnValue(
      new Promise<api.EcoImpact>((_r, rj) => {
        reject = rj;
      })
    );
    const { container, unmount } = render(<Metrics />);
    unmount();
    await act(async () => {
      reject(new Error("eco lookup died"));
    });
    expect(container.innerHTML).toBe("");
    expect(screen.queryByText(/Eco Mode impact/i)).not.toBeInTheDocument();
  });

  it("drops a summary failure that lands after the page unmounted", async () => {
    let reject!: (e: unknown) => void;
    vi.mocked(api.getMetricsHomeSummary).mockReturnValue(
      new Promise<api.MetricsSummary>((_r, rj) => {
        reject = rj;
      })
    );
    const { container, unmount } = render(<Metrics />);
    unmount();
    await act(async () => {
      reject(new Error("late failure"));
    });
    expect(container.innerHTML).toBe("");
    expect(screen.queryByText("late failure")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Empty-state banners
// ---------------------------------------------------------------------------

describe("EmptyStateBanners", () => {
  it("shows neither banner when there is data and an outside sensor", async () => {
    render(<Metrics />);
    await screen.findByText("Heating time");
    expect(screen.queryByText(/No cycle data yet for this range/i)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/Outside-temperature sensor not configured/i)
    ).not.toBeInTheDocument();
  });

  it("shows the no-data banner when the range holds no cycles", async () => {
    vi.mocked(api.getMetricsHomeSummary).mockResolvedValue({ ...baseSummary, cycle_count: 0 });
    render(<Metrics />);
    expect(await screen.findByText(/No cycle data yet for this range/i)).toBeInTheDocument();
    expect(
      screen.queryByText(/Outside-temperature sensor not configured/i)
    ).not.toBeInTheDocument();
  });

  it("shows the outside-sensor banner when no entity is configured", async () => {
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: null,
      current_value: null,
    });
    render(<Metrics />);
    expect(
      await screen.findByText(/Outside-temperature sensor not configured/i)
    ).toBeInTheDocument();
    expect(screen.queryByText(/No cycle data yet for this range/i)).not.toBeInTheDocument();
  });

  it("shows both banners when there are no cycles and no outside sensor", async () => {
    vi.mocked(api.getMetricsHomeSummary).mockResolvedValue({ ...baseSummary, cycle_count: 0 });
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: null,
      current_value: null,
    });
    render(<Metrics />);
    expect(await screen.findByText(/No cycle data yet for this range/i)).toBeInTheDocument();
    expect(
      await screen.findByText(/Outside-temperature sensor not configured/i)
    ).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Eco impact tiles
// ---------------------------------------------------------------------------

describe("EcoImpactSection", () => {
  it("reports a 0.0% runtime share instead of NaN when no runtime was logged", async () => {
    vi.mocked(api.getMetricsEcoImpact).mockResolvedValue({
      ...baseEco,
      total_cycles: 5,
      eco_active_cycles: 2,
      total_seconds: 0,
      eco_active_seconds: 0,
    });
    render(<Metrics />);
    await screen.findByText("Eco runtime share");
    const tile = screen.getByText("Eco runtime share").parentElement!;
    expect(tile).toHaveTextContent("0.0%");
    expect(tile).not.toHaveTextContent("NaN");
  });

  it("converts the avg-drift tile with the DELTA conversion in Celsius", async () => {
    render(
      <UnitContext.Provider value={buildUnitContext("C")}>
        <Metrics />
      </UnitContext.Provider>
    );
    await screen.findByText("Avg drift applied");
    // 2.5 °F of relaxation is a delta → 1.39 °C, never the absolute −16.39 °C.
    expect(screen.getByText("1.39°C")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Thermostat selector labelling
// ---------------------------------------------------------------------------

describe("thermostat labelling", () => {
  it("falls back to the entity id in the option and the subtitle when a thermostat is unnamed", async () => {
    vi.mocked(api.getThermostats).mockResolvedValue([
      thermostat({ thermostat_entity_id: "climate.unnamed", name: "" }),
    ]);
    render(<Metrics />);
    const select = await screen.findByDisplayValue(/All thermostats/i);
    expect(await screen.findByRole("option", { name: "climate.unnamed" })).toBeInTheDocument();

    fireEvent.change(select, { target: { value: "climate.unnamed" } });
    expect(await screen.findByText(/Showing climate\.unnamed for/)).toBeInTheDocument();
  });

  it("uses the friendly name in the subtitle when one is set", async () => {
    render(<Metrics />);
    const select = await screen.findByDisplayValue(/All thermostats/i);
    fireEvent.change(select, { target: { value: "climate.test" } });
    expect(await screen.findByText(/Showing Main HVAC for/)).toBeInTheDocument();
  });
});
