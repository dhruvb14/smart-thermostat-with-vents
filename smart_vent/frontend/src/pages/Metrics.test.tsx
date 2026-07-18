import { describe, it, expect, vi, beforeEach } from "vitest";
import { ecoThermostatDefaults } from "../testFixtures";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import Metrics from "./Metrics";
import * as api from "../api";

vi.mock("../api");

// recharts' ResponsiveContainer reports a 0×0 box under jsdom and logs
// "width(0)/height(0)". This page renders the chart grid, so stub the wrapper
// to a fixed size to keep the test log clean. (Chart code paths are covered by
// the dedicated MetricsCharts.test.tsx.)
vi.mock("recharts", async () => {
  const actual = await vi.importActual<typeof import("recharts")>("recharts");
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
      <div style={{ width: 800, height: 300 }}>{children}</div>
    ),
  };
});

const mockSummary: api.MetricsSummary = {
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
  source_breakdown: { schedule: 7, presence: 3 },
  avg_outside_temp_at_start: 45.0,
  avg_outside_temp_at_end: 46.5,
  avg_cycle_duration_seconds: 1800,
  thermostat_count: 1,
  eco_cycle_count: 2,
  eco_seconds: 3000,
};

const mockEcoImpact: api.EcoImpact = {
  start_date: "2024-01-01",
  end_date: "2024-01-07",
  thermostat_entity_id: null,
  total_cycles: 10,
  total_seconds: 10800,
  eco_active_cycles: 2,
  eco_active_seconds: 3000,
  avg_drift_f: 2.5,
  days: [
    {
      date: "2024-01-06",
      total_cycles: 5,
      total_seconds: 5400,
      eco_active_cycles: 2,
      eco_active_seconds: 3000,
      avg_drift_f: 2.5,
    },
  ],
  rooms: [
    { room_id: "r1", name: "Living Room", eco_active_cycles: 2, avg_drift_f: 2.5, max_drift_f: 4 },
  ],
};

describe("Metrics Page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getThermostats).mockResolvedValue([
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
    ]);
    vi.mocked(api.getMetricsHomeSummary).mockResolvedValue(mockSummary);
    vi.mocked(api.getMetricsThermostatSummary).mockResolvedValue({
      ...mockSummary,
      thermostat_entity_id: "climate.test",
    });
    vi.mocked(api.getMetricsEcoImpact).mockResolvedValue(mockEcoImpact);
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: "sensor.outside_temp",
      current_value: 42,
    });
    vi.mocked(api.getHAEntities).mockResolvedValue([
      { entity_id: "sensor.outside_temp", friendly_name: "Outside Temp", state: "42" },
    ]);
    vi.mocked(api.setOutsideTempEntity).mockResolvedValue({
      entity_id: "sensor.outside_temp",
      current_value: 42,
    });

    // Default mocks for charts to prevent "then" of undefined
    vi.mocked(api.getMetricsTimeseries).mockResolvedValue({
      series: [],
      thermostat_entity_id: "climate.test",
      metric: "hours",
      granularity: "day",
      start: "",
      end: "",
    });
    vi.mocked(api.getMetricsCyclesVsOutsideTemp).mockResolvedValue({
      points: [],
      thermostat_entity_id: "climate.test",
      start: "",
      end: "",
    });
    vi.mocked(api.getMetricsRoomBreakdown).mockResolvedValue({
      rooms: [],
      thermostat_entity_id: "climate.test",
      start: "",
      end: "",
    });
    vi.mocked(api.getMetricsOvershootHistogram).mockResolvedValue({
      labels: [],
      counts: [],
      total_room_cycles: 0,
      thermostat_entity_id: "climate.test",
      start_date: "",
      end_date: "",
      bin_size: 1,
      overshot_count: 0,
      overshot_pct: 0,
      max_overshoot_f: 0,
      avg_overshoot_f: 0,
    });
    vi.mocked(api.getMetricsHourHeatmap).mockResolvedValue({
      grid_seconds: [],
      day_labels: [],
      thermostat_entity_id: "climate.test",
      start_date: "",
      end_date: "",
    });
    vi.mocked(api.getMetricsVentTimeline).mockResolvedValue({
      events: [],
      note: "",
      thermostat_entity_id: "climate.test",
      start: "",
      end: "",
    });
  });

  it("renders the metrics page and summary stats", async () => {
    render(<Metrics />);
    expect(await screen.findByText(/Metrics/i)).toBeInTheDocument();
    expect(await screen.findByText("1h 0m")).toBeInTheDocument(); // heating
    expect(await screen.findByText("2h 0m")).toBeInTheDocument(); // cooling
    expect(await screen.findByText("15.5%")).toBeInTheDocument(); // duty cycle
  });

  it("changes thermostat filter", async () => {
    render(<Metrics />);

    // Select element doesn't have a label but we can find by display value or tag
    const select = await screen.findByDisplayValue(/All thermostats/i);
    fireEvent.change(select, { target: { value: "climate.test" } });

    await waitFor(() => {
      expect(api.getMetricsThermostatSummary).toHaveBeenCalledWith(
        "climate.test",
        expect.anything()
      );
    });
  });

  it("handles CSV export", async () => {
    vi.mocked(api.downloadMetricsCsv).mockReturnValue(undefined);
    render(<Metrics />);

    const exportBtn = await screen.findByText(/Export CSV/i);
    fireEvent.click(exportBtn);

    expect(api.downloadMetricsCsv).toHaveBeenCalled();
  });

  it("renders the Eco Mode impact section with tiles and charts (Issue #442)", async () => {
    render(<Metrics />);
    expect(await screen.findByText(/🌿 Eco Mode impact/i)).toBeInTheDocument();
    expect(await screen.findByText("2 of 10")).toBeInTheDocument(); // eco-relaxed cycles
    expect(await screen.findByText("20.0% of cycles")).toBeInTheDocument();
    expect(await screen.findByText("27.8%")).toBeInTheDocument(); // runtime share 3000/10800
    expect(await screen.findByText("2.50°F")).toBeInTheDocument(); // avg drift, °F mode
    // Estimated savings: 2.5°F × 3–5%/°F, explicitly labeled an estimate.
    expect(await screen.findByText("≈7.5–12.5%")).toBeInTheDocument();
    expect(await screen.findByText(/rule of thumb.*not measured/i)).toBeInTheDocument();
    expect(await screen.findByText(/Eco-relaxed vs standard cycles/i)).toBeInTheDocument();
    expect(await screen.findByText(/Average Eco drift applied/i)).toBeInTheDocument();
    expect(await screen.findByText(/Eco drift by room/i)).toBeInTheDocument();
  });

  it("shows the zero-engagement note when Eco never relaxed a cycle", async () => {
    vi.mocked(api.getMetricsEcoImpact).mockResolvedValue({
      ...mockEcoImpact,
      eco_active_cycles: 0,
      eco_active_seconds: 0,
      days: [],
      rooms: [],
    });
    render(<Metrics />);
    expect(await screen.findByText(/No Eco-relaxed cycles in this range/i)).toBeInTheDocument();
    expect(screen.queryByText(/Est. energy saved/i)).not.toBeInTheDocument();
  });

  it("hides the eco section entirely when there is no cycle data at all", async () => {
    vi.mocked(api.getMetricsEcoImpact).mockResolvedValue({
      ...mockEcoImpact,
      total_cycles: 0,
      total_seconds: 0,
      eco_active_cycles: 0,
      eco_active_seconds: 0,
      days: [],
      rooms: [],
    });
    render(<Metrics />);
    await screen.findByText(/Metrics/i);
    await waitFor(() => {
      expect(api.getMetricsEcoImpact).toHaveBeenCalled();
    });
    expect(screen.queryByText(/🌿 Eco Mode impact/i)).not.toBeInTheDocument();
  });

  it("renders no eco section when the eco-impact fetch fails", async () => {
    vi.mocked(api.getMetricsEcoImpact).mockRejectedValue(new Error("boom"));
    render(<Metrics />);
    await screen.findByText("Heating time");
    expect(screen.queryByText(/🌿 Eco Mode impact/i)).not.toBeInTheDocument();
  });

  it("refetches when the date range is edited and resets via Last 7 days", async () => {
    const { container } = render(<Metrics />);
    await screen.findByText(/Export CSV/i);
    const [startInput, endInput] = Array.from(
      container.querySelectorAll<HTMLInputElement>('input[type="date"]')
    );
    const initialStart = startInput.value;

    fireEvent.change(startInput, { target: { value: "2024-02-01" } });
    fireEvent.change(endInput, { target: { value: "2024-02-15" } });
    await waitFor(() => {
      expect(api.getMetricsHomeSummary).toHaveBeenCalledWith(
        expect.objectContaining({ start: "2024-02-01", end: "2024-02-15" })
      );
    });
    expect(screen.getByText(/2024-02-01 → 2024-02-15/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Last 7 days/i }));
    await waitFor(() => expect(startInput.value).toBe(initialStart));
  });

  it("exports CSV scoped to the selected thermostat and range", async () => {
    vi.mocked(api.downloadMetricsCsv).mockReturnValue(undefined);
    render(<Metrics />);
    const select = await screen.findByDisplayValue(/All thermostats/i);
    fireEvent.change(select, { target: { value: "climate.test" } });
    fireEvent.click(await screen.findByText(/Export CSV/i));
    expect(api.downloadMetricsCsv).toHaveBeenCalledWith(
      expect.objectContaining({ start: expect.any(String), end: expect.any(String) }),
      "thermostat",
      "climate.test"
    );
  });

  it("requests the per-thermostat eco impact when a thermostat is selected", async () => {
    render(<Metrics />);
    const select = await screen.findByDisplayValue(/All thermostats/i);
    fireEvent.change(select, { target: { value: "climate.test" } });
    await waitFor(() => {
      expect(api.getMetricsEcoImpact).toHaveBeenCalledWith("climate.test", expect.anything());
    });
  });
});
