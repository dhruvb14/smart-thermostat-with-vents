import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import * as api from "../../api";
import {
  HeatingCoolingHoursChart,
  CyclesPerDayChart,
  AvgCycleDurationChart,
  CyclesVsOutsideTempChart,
  DutyCycleChart,
  TimeToTargetChart,
  CompletionRateChart,
  SourceBreakdownChart,
  PerRoomHeatingCoolingChart,
  RoomParticipationChart,
  DegreeMinutesChart,
  OvershootHistogramChart,
  HourHeatmapChart,
  VentTimelineChart,
  ChartGrid,
} from "./MetricsCharts";
import { UnitContext, buildUnitContext } from "../../contexts";

vi.mock("../../api");

// recharts' ResponsiveContainer measures its parent via ResizeObserver, which
// reports a 0×0 box in jsdom — recharts then refuses to render and logs
// "width(0)/height(0)". Mock just that wrapper to a fixed-size box so the real
// chart children (BarChart, XAxis, formatters, …) render and their code paths
// are covered. Scoped to this file so other suites keep the real recharts.
vi.mock("recharts", async () => {
  const actual = await vi.importActual<typeof import("recharts")>("recharts");
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
      <div style={{ width: 800, height: 300 }}>{children}</div>
    ),
  };
});

const entityId = "climate.test";
const range = { start: "2024-01-01", end: "2024-01-07" };

const timeseries = (metric: api.MetricsTimeseriesMetric): api.MetricsTimeseries => ({
  thermostat_entity_id: entityId,
  metric,
  granularity: "day",
  start: range.start,
  end: range.end,
  series: [
    { period: "2024-01-01", value: 120, heating_seconds: 3600, cooling_seconds: 1800 },
    { period: "2024-01-02", value: null, heating_seconds: 0, cooling_seconds: 7200 },
  ],
});

const summary: api.MetricsSummary = {
  start_date: range.start,
  end_date: range.end,
  thermostat_entity_id: entityId,
  heating_seconds: 3600,
  cooling_seconds: 7200,
  cycle_count: 10,
  completed_count: 8,
  timeout_count: 1,
  aborted_count: 1,
  avg_cycle_duration_seconds: 1800,
  duty_cycle_pct: 15.5,
  avg_outside_temp_at_start: 45,
  avg_outside_temp_at_end: 46,
  thermostat_count: 1,
  source_breakdown: { schedule: 7, presence: 3, override: 1 },
};

const renderWithUnit = (ui: React.ReactElement, unit: "F" | "C" = "F") =>
  render(<UnitContext.Provider value={buildUnitContext(unit)}>{ui}</UnitContext.Provider>);

describe("MetricsCharts", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getMetricsTimeseries).mockImplementation(async (_e, metric) =>
      timeseries(metric)
    );
    vi.mocked(api.getMetricsThermostatSummary).mockResolvedValue(summary);
    vi.mocked(api.getMetricsCyclesVsOutsideTemp).mockResolvedValue({
      thermostat_entity_id: entityId,
      start: range.start,
      end: range.end,
      points: [
        {
          cycle_id: "c1",
          mode: "heating",
          outside_temp: 40,
          outside_temp_at_end: 42,
          duration_minutes: 30,
          started_at: "2024-01-01T12:00:00",
        },
        {
          cycle_id: "c2",
          mode: "cooling",
          outside_temp: 80,
          outside_temp_at_end: 78,
          duration_minutes: 45,
          started_at: "2024-01-02T12:00:00",
        },
      ],
    });
    vi.mocked(api.getMetricsRoomBreakdown).mockResolvedValue({
      thermostat_entity_id: entityId,
      start: range.start,
      end: range.end,
      rooms: [
        {
          room_id: "r1",
          room_name: "Living Room",
          participation_count: 5,
          participation_rate: 0.5,
          heating_seconds: 3600,
          cooling_seconds: 1800,
          avg_time_to_target_seconds: 600,
        },
      ],
    });
    vi.mocked(api.getMetricsOvershootHistogram).mockResolvedValue({
      thermostat_entity_id: entityId,
      start_date: range.start,
      end_date: range.end,
      bin_size: 1,
      labels: ["0-1", "1-2"],
      counts: [3, 1],
      total_room_cycles: 10,
      overshot_count: 4,
      overshot_pct: 40,
      max_overshoot_f: 2.5,
      avg_overshoot_f: 1.2,
    });
    vi.mocked(api.getMetricsHourHeatmap).mockResolvedValue({
      thermostat_entity_id: entityId,
      start_date: range.start,
      end_date: range.end,
      day_labels: ["Mon", "Tue"],
      grid_seconds: [
        Array.from({ length: 24 }, (_, h) => h * 60),
        Array.from({ length: 24 }, () => 0),
      ],
    });
    vi.mocked(api.getMetricsVentTimeline).mockResolvedValue({
      thermostat_entity_id: entityId,
      start: range.start,
      end: range.end,
      note: "Showing the most recent 200 events.",
      events: [
        {
          cycle_id: "c1",
          timestamp: "2024-01-01T12:00:00",
          entity_id: "cover.living",
          room_id: "r1",
          action: "open",
          reason: "cycle start",
          cycle_mode: "heating",
          cycle_started_at: "2024-01-01T12:00:00",
          cycle_ended_at: "2024-01-01T13:00:00",
        },
      ],
    });
  });

  it("renders the heating/cooling hours chart", async () => {
    renderWithUnit(<HeatingCoolingHoursChart entityId={entityId} range={range} />);
    expect(await screen.findByText(/Heating & cooling hours per day/i)).toBeInTheDocument();
  });

  it("renders the cycles-per-day chart", async () => {
    renderWithUnit(<CyclesPerDayChart entityId={entityId} range={range} />);
    expect(await screen.findByText(/Cycles per day/i)).toBeInTheDocument();
  });

  it("renders the average cycle duration chart", async () => {
    renderWithUnit(<AvgCycleDurationChart entityId={entityId} range={range} />);
    expect(await screen.findByText(/Average cycle duration/i)).toBeInTheDocument();
  });

  it("renders the cycles-vs-outside-temp scatter, converting to display units", async () => {
    renderWithUnit(<CyclesVsOutsideTempChart entityId={entityId} range={range} />, "C");
    expect(await screen.findByText(/Cycles vs outside temperature/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(api.getMetricsCyclesVsOutsideTemp).toHaveBeenCalled();
    });
  });

  it("renders the duty-cycle chart", async () => {
    renderWithUnit(<DutyCycleChart entityId={entityId} range={range} />);
    expect(await screen.findByText(/Duty cycle/i)).toBeInTheDocument();
  });

  it("renders the time-to-target chart", async () => {
    renderWithUnit(<TimeToTargetChart entityId={entityId} range={range} />);
    expect(await screen.findByText(/Time to target/i)).toBeInTheDocument();
  });

  it("renders the completion-rate donut from a summary", () => {
    renderWithUnit(<CompletionRateChart summary={summary} loading={false} />);
    expect(screen.getByText(/Cycle completion rate/i)).toBeInTheDocument();
    expect(screen.getByText(/completed/i)).toBeInTheDocument();
  });

  it("shows the completion-rate empty state with no cycles", () => {
    renderWithUnit(
      <CompletionRateChart
        summary={{ ...summary, completed_count: 0, timeout_count: 0, aborted_count: 0 }}
        loading={false}
      />
    );
    expect(screen.getByText(/No data for this range yet/i)).toBeInTheDocument();
  });

  it("renders the source-breakdown donut", () => {
    renderWithUnit(<SourceBreakdownChart summary={summary} loading={false} />);
    expect(screen.getByText(/Source breakdown/i)).toBeInTheDocument();
  });

  it("renders the per-room heating/cooling chart", async () => {
    renderWithUnit(<PerRoomHeatingCoolingChart entityId={entityId} range={range} />);
    expect(await screen.findByText(/Per-room heating vs cooling/i)).toBeInTheDocument();
  });

  it("renders the room-participation chart", async () => {
    renderWithUnit(<RoomParticipationChart entityId={entityId} range={range} />);
    expect(await screen.findByText(/Room participation rate/i)).toBeInTheDocument();
  });

  it("renders the degree-minutes chart", async () => {
    renderWithUnit(<DegreeMinutesChart entityId={entityId} range={range} />);
    expect(await screen.findByText(/Degree-minutes/i)).toBeInTheDocument();
  });

  it("renders the overshoot histogram with a computed subtitle", async () => {
    renderWithUnit(<OvershootHistogramChart entityId={entityId} range={range} />);
    expect(await screen.findByText(/Overshoot histogram/i)).toBeInTheDocument();
    expect(await screen.findByText(/room-cycles overshot/i)).toBeInTheDocument();
  });

  it("renders the hour-of-day heatmap grid", async () => {
    renderWithUnit(<HourHeatmapChart entityId={entityId} range={range} />);
    expect(await screen.findByText(/Hour-of-day heatmap/i)).toBeInTheDocument();
    expect(await screen.findByText("Mon")).toBeInTheDocument();
  });

  it("shows the heatmap empty state when all cells are zero", async () => {
    vi.mocked(api.getMetricsHourHeatmap).mockResolvedValue({
      thermostat_entity_id: entityId,
      start_date: range.start,
      end_date: range.end,
      day_labels: ["Mon"],
      grid_seconds: [Array.from({ length: 24 }, () => 0)],
    });
    renderWithUnit(<HourHeatmapChart entityId={entityId} range={range} />);
    expect(await screen.findByText(/No HVAC activity recorded/i)).toBeInTheDocument();
  });

  it("renders the vent timeline table and note", async () => {
    renderWithUnit(<VentTimelineChart entityId={entityId} range={range} />);
    expect(await screen.findByText(/Vent timeline/i)).toBeInTheDocument();
    expect(await screen.findByText("cover.living")).toBeInTheDocument();
    expect(screen.getByText(/most recent 200 events/i)).toBeInTheDocument();
  });

  it("shows the vent-timeline empty state with no events", async () => {
    vi.mocked(api.getMetricsVentTimeline).mockResolvedValue({
      thermostat_entity_id: entityId,
      start: range.start,
      end: range.end,
      note: "",
      events: [],
    });
    renderWithUnit(<VentTimelineChart entityId={entityId} range={range} />);
    expect(await screen.findByText(/No vent events recorded/i)).toBeInTheDocument();
  });

  describe("ChartGrid", () => {
    it("renders the two home donuts in home mode", () => {
      renderWithUnit(
        <ChartGrid entityId={null} range={range} homeSummary={summary} homeLoading={false} isHome />
      );
      expect(screen.getByText(/Cycle completion rate/i)).toBeInTheDocument();
      expect(screen.getByText(/Source breakdown/i)).toBeInTheDocument();
    });

    it("renders the per-thermostat charts when an entity is selected", async () => {
      renderWithUnit(
        <ChartGrid
          entityId={entityId}
          range={range}
          homeSummary={null}
          homeLoading={false}
          isHome={false}
        />
      );
      expect(await screen.findByText(/Heating & cooling hours per day/i)).toBeInTheDocument();
      expect(await screen.findByText(/Vent timeline/i)).toBeInTheDocument();
    });

    it("renders nothing when not home and no entity is selected", () => {
      const { container } = renderWithUnit(
        <ChartGrid
          entityId={null}
          range={range}
          homeSummary={null}
          homeLoading={false}
          isHome={false}
        />
      );
      // Only the empty grid wrapper, no chart cards
      expect(container.querySelectorAll(".chart-card").length).toBe(0);
    });
  });
});
