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
  EcoCyclesPerDayChart,
  EcoDriftPerDayChart,
  EcoRoomDriftChart,
  OutsideTempPerDayChart,
  ShortCyclesChart,
  ChartGrid,
} from "./MetricsCharts";
import { fmtOvershootDelta, localizeBinLabel, degreeMinutesSeries } from "./format";
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
  eco_cycle_count: 2,
  eco_seconds: 3000,
};

const ecoImpact: api.EcoImpact = {
  start_date: range.start,
  end_date: range.end,
  thermostat_entity_id: entityId,
  total_cycles: 10,
  total_seconds: 10800,
  eco_active_cycles: 3,
  eco_active_seconds: 3600,
  avg_drift_f: 2.0,
  days: [
    {
      date: "2024-01-01",
      total_cycles: 5,
      total_seconds: 5400,
      eco_active_cycles: 0,
      eco_active_seconds: 0,
      avg_drift_f: 0,
    },
    {
      date: "2024-01-02",
      total_cycles: 5,
      total_seconds: 5400,
      eco_active_cycles: 3,
      eco_active_seconds: 3600,
      avg_drift_f: 2.0,
    },
  ],
  rooms: [
    { room_id: "r1", name: "Living Room", eco_active_cycles: 3, avg_drift_f: 2.0, max_drift_f: 4 },
    { room_id: "r2", name: null, eco_active_cycles: 1, avg_drift_f: 1.0, max_drift_f: 1 },
  ],
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
        {
          cycle_id: "c3",
          mode: "cooling",
          outside_temp: 95,
          outside_temp_at_end: 94,
          duration_minutes: 25,
          started_at: "2024-01-03T12:00:00",
          eco_active: true,
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

  it("splits eco-relaxed cycles into their own scatter series (Issue #442)", async () => {
    renderWithUnit(<CyclesVsOutsideTempChart entityId={entityId} range={range} />);
    await screen.findByText(/Cycles vs outside temperature/i);
    // jsdom doesn't lay out the recharts SVG, so legend text isn't queryable;
    // the subtitle documents the third (eco) series instead.
    expect(screen.getByText(/Green = Eco-relaxed/i)).toBeInTheDocument();
  });

  it("renders the eco cycles-per-day stacked bars from an impact payload", () => {
    renderWithUnit(<EcoCyclesPerDayChart impact={ecoImpact} loading={false} />);
    expect(screen.getByText(/Eco-relaxed vs standard cycles/i)).toBeInTheDocument();
    expect(
      screen.getByText(/Cycles per day where Eco Mode relaxed at least one room/i)
    ).toBeInTheDocument();
  });

  it("shows the eco cycles empty state without day data", () => {
    renderWithUnit(<EcoCyclesPerDayChart impact={{ ...ecoImpact, days: [] }} loading={false} />);
    expect(screen.getByText(/No data for this range yet/i)).toBeInTheDocument();
  });

  it("renders the eco drift-per-day line with the delta conversion in Celsius (Issue #291 pattern)", () => {
    // 2.0°F drift is a DELTA → 1.11°C, never the absolute −16.7°C.
    renderWithUnit(<EcoDriftPerDayChart impact={ecoImpact} loading={false} />, "C");
    expect(screen.getByText(/Average Eco drift applied/i)).toBeInTheDocument();
  });

  it("renders the per-room eco drift bars", () => {
    renderWithUnit(<EcoRoomDriftChart impact={ecoImpact} loading={false} />);
    expect(screen.getByText(/Eco drift by room/i)).toBeInTheDocument();
    expect(
      screen.getByText(/Average and peak relaxation applied to each room/i)
    ).toBeInTheDocument();
  });

  it("shows the per-room eco drift empty state when no room was relaxed", () => {
    renderWithUnit(<EcoRoomDriftChart impact={{ ...ecoImpact, rooms: [] }} loading={false} />);
    expect(screen.getByText(/No Eco-relaxed room-cycles in this range/i)).toBeInTheDocument();
  });

  it("renders the outside-temperature-per-day line", async () => {
    renderWithUnit(<OutsideTempPerDayChart entityId={entityId} range={range} />);
    expect(await screen.findByText(/Outside temperature/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(api.getMetricsTimeseries).toHaveBeenCalledWith(entityId, "outside_temp", "day", range);
    });
  });

  it("shows the outside-temperature empty state when every day is null", async () => {
    vi.mocked(api.getMetricsTimeseries).mockResolvedValue({
      thermostat_entity_id: entityId,
      metric: "outside_temp",
      granularity: "day",
      start: range.start,
      end: range.end,
      series: [{ period: "2024-01-01", value: null }],
    });
    renderWithUnit(<OutsideTempPerDayChart entityId={entityId} range={range} />);
    expect(await screen.findByText(/No outside-temp data yet/i)).toBeInTheDocument();
  });

  it("renders the short-cycles chart", async () => {
    renderWithUnit(<ShortCyclesChart entityId={entityId} range={range} />);
    expect(await screen.findByText(/Short cycles/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(api.getMetricsTimeseries).toHaveBeenCalledWith(entityId, "short_cycles", "day", range);
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

  it("includes the safety source in the breakdown (Issue #367)", () => {
    const withSafety: api.MetricsSummary = {
      ...summary,
      source_breakdown: { schedule: 4, presence: 2, override: 1, safety: 3 },
    };
    renderWithUnit(<SourceBreakdownChart summary={withSafety} loading={false} />);
    // The chart maps every source — including the new "safety" key — to a
    // colour; this exercises that branch so safety renders distinctly.
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

  it("renders vent timeline timestamps as localized time, not the raw naive-UTC string (Issue #301)", async () => {
    // Backend vent-event timestamps are naive-UTC ISO strings. They must be
    // parsed as UTC (append "Z") and localized, matching the Logs/DevMode views.
    renderWithUnit(<VentTimelineChart entityId={entityId} range={range} />);
    await screen.findByText(/Vent timeline/i);
    const localized = new Date("2024-01-01T12:00:00Z").toLocaleString();
    expect(screen.getByText(localized)).toBeInTheDocument();
    // The raw, un-localized ISO string must NOT be shown.
    expect(screen.queryByText("2024-01-01T12:00:00")).not.toBeInTheDocument();
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

// ---------------------------------------------------------------------------
// Celsius delta conversion for chart magnitudes (Issues #291, #292)
// ---------------------------------------------------------------------------

const C = buildUnitContext("C");
const F = buildUnitContext("F");

describe("fmtOvershootDelta (Issue #291)", () => {
  it("converts a 2°F overshoot DELTA to +1.1°C, not the absolute −16.7°C", () => {
    expect(fmtOvershootDelta(2, C.toDisplayDelta, C.unitLabel)).toBe("1.1°C");
  });
  it("is identity (°F label) in Fahrenheit mode", () => {
    expect(fmtOvershootDelta(2, F.toDisplayDelta, F.unitLabel)).toBe("2.0°F");
  });
  it("renders an em-dash for null", () => {
    expect(fmtOvershootDelta(null, C.toDisplayDelta, C.unitLabel)).toBe("—");
  });
});

describe("localizeBinLabel (Issue #291)", () => {
  it("converts °F bin boundaries to °C deltas in Celsius mode", () => {
    expect(localizeBinLabel("0–1°F", C.toDisplayDelta, C.unitLabel, true)).toBe("0.0–0.6°C");
    expect(localizeBinLabel("≥5°F", C.toDisplayDelta, C.unitLabel, true)).toBe("≥2.8°C");
  });
  it("leaves backend °F labels unchanged in Fahrenheit mode", () => {
    expect(localizeBinLabel("0–1°F", F.toDisplayDelta, F.unitLabel, false)).toBe("0–1°F");
  });
});

describe("degreeMinutesSeries (Issue #292)", () => {
  it("scales °F·min magnitudes by the DELTA conversion in Celsius mode", () => {
    const out = degreeMinutesSeries([{ period: "2024-01-02", value: 90 }], C.toDisplayDelta);
    expect(out[0].value).toBeCloseTo(50, 5); // 90 × 5/9
  });
  it("is identity in Fahrenheit mode", () => {
    const out = degreeMinutesSeries([{ period: "2024-01-02", value: 90 }], F.toDisplayDelta);
    expect(out[0].value).toBeCloseTo(90, 5);
  });
});

describe("OvershootHistogramChart subtitle in Celsius (Issue #291)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getMetricsOvershootHistogram).mockResolvedValue({
      thermostat_entity_id: entityId,
      start_date: range.start,
      end_date: range.end,
      bin_size: 1,
      labels: ["0–1°F", "1–2°F", "≥2°F"],
      counts: [3, 1, 0],
      total_room_cycles: 4,
      overshot_count: 1,
      overshot_pct: 25,
      max_overshoot_f: 2,
      avg_overshoot_f: 1,
    });
  });

  it("shows a positive °C overshoot, never the negative absolute conversion", async () => {
    render(
      <UnitContext.Provider value={C}>
        <OvershootHistogramChart entityId={entityId} range={range} />
      </UnitContext.Provider>
    );
    await screen.findByText(/room-cycles overshot/i);
    const subtitle = screen.getByText(/room-cycles overshot/i).textContent ?? "";
    expect(subtitle).toContain("max 1.1°C"); // 2 × 5/9
    expect(subtitle).toContain("avg 0.6°C"); // 1 × 5/9
    expect(subtitle).not.toContain("-16"); // the buggy absolute conversion
  });
});
