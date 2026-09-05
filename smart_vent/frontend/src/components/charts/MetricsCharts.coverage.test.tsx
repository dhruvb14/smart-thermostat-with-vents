/**
 * Coverage companion to MetricsCharts.test.tsx.
 *
 * Two mechanics make the otherwise-unreachable chart internals testable:
 *
 * 1. `ResponsiveContainer` is replaced with one that clones the chart element
 *    with an explicit 800×300 box. jsdom reports a 0×0 parent, and recharts
 *    then renders nothing at all — so axis `tickFormatter`s never run. With a
 *    real box, recharts performs its real layout and the tick text lands in
 *    the DOM where it can be asserted.
 * 2. Recharts 3 ships a keyboard accessibility layer: focusing the chart
 *    surface and pressing ArrowRight activates a data index, which renders the
 *    tooltip — and therefore runs the `Tooltip formatter` callbacks. No mouse
 *    coordinates (which jsdom cannot supply) are needed.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { cloneElement } from "react";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import * as api from "../../api";
import {
  HeatingCoolingHoursChart,
  AvgCycleDurationChart,
  CyclesVsOutsideTempChart,
  DutyCycleChart,
  TimeToTargetChart,
  CompletionRateChart,
  PerRoomHeatingCoolingChart,
  RoomParticipationChart,
  DegreeMinutesChart,
  OvershootHistogramChart,
  VentTimelineChart,
  EcoCyclesPerDayChart,
  EcoDriftPerDayChart,
  EcoRoomDriftChart,
} from "./MetricsCharts";
import { COLORS } from "./colors";
import { UnitContext, buildUnitContext } from "../../contexts";

vi.mock("../../api");

vi.mock("recharts", async () => {
  const actual = await vi.importActual<typeof import("recharts")>("recharts");
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactElement }) =>
      cloneElement(children as React.ReactElement<{ width?: number; height?: number }>, {
        width: 800,
        height: 300,
      }),
  };
});

const entityId = "climate.test";
const range = { start: "2024-01-01", end: "2024-01-07" };

const renderWithUnit = (ui: React.ReactElement, unit: "F" | "C" = "F") =>
  render(<UnitContext.Provider value={buildUnitContext(unit)}>{ui}</UnitContext.Provider>);

/** Wait until recharts has actually laid the chart out (the container title
 * renders during loading too, so `findByText` on it is not enough). */
async function chartLaidOut(container: HTMLElement) {
  await waitFor(() => {
    if (!container.querySelector(".recharts-surface")) throw new Error("chart not laid out yet");
  });
}

/**
 * Focus the chart surface and walk the keyboard cursor across `steps` data
 * indices, returning every tooltip rendering seen along the way joined with
 * " | ". Walking (rather than pinning one index) keeps the assertions
 * independent of which index recharts activates first.
 */
function tooltipText(container: HTMLElement, steps = 1): string {
  const surface = container.querySelector(".recharts-surface");
  if (!surface) throw new Error("chart surface never rendered — layout mock broken");
  const read = () => container.querySelector(".recharts-tooltip-wrapper")?.textContent ?? "";
  fireEvent.focus(surface);
  const seen = [read()];
  for (let i = 0; i < steps; i++) {
    fireEvent.keyDown(surface, { key: "ArrowRight" });
    seen.push(read());
  }
  return seen.join(" | ");
}

/** The rendered axis tick labels, in DOM order. */
function tickLabels(container: HTMLElement, axis: "xAxis" | "yAxis"): string[] {
  return Array.from(
    container.querySelectorAll(`.recharts-${axis}-tick-labels .recharts-cartesian-axis-tick-value`)
  ).map((n) => n.textContent ?? "");
}

const ts = (
  series: api.MetricsTimeseries["series"],
  metric: api.MetricsTimeseriesMetric = "hours"
): api.MetricsTimeseries => ({
  thermostat_entity_id: entityId,
  metric,
  granularity: "day",
  start: range.start,
  end: range.end,
  series,
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
  source_breakdown: { schedule: 7 },
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
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// useFetch — the rejection path (nothing in the file renders `error`, so the
// observable contract is "a failed fetch degrades to the empty state").
// ---------------------------------------------------------------------------

describe("useFetch rejection handling", () => {
  it("falls back to the empty state (not a spinner or a crash) when the fetch rejects with an Error", async () => {
    vi.mocked(api.getMetricsTimeseries).mockRejectedValue(new Error("backend exploded"));
    renderWithUnit(<HeatingCoolingHoursChart entityId={entityId} range={range} />);
    expect(await screen.findByText(/No data for this range yet/i)).toBeInTheDocument();
    // The loading skeleton must be gone — `.finally` has to run even on reject.
    expect(document.querySelector(".skeleton-bar")).toBeNull();
  });

  it("falls back to the empty state when the fetch rejects with a non-Error value", async () => {
    vi.mocked(api.getMetricsTimeseries).mockRejectedValue("just a string");
    renderWithUnit(<DutyCycleChart entityId={entityId} range={range} />);
    expect(await screen.findByText(/No data for this range yet/i)).toBeInTheDocument();
    expect(document.querySelector(".skeleton-bar")).toBeNull();
  });
});

describe("useFetch cancellation", () => {
  const laterRange = { start: "2024-02-01", end: "2024-02-07" };

  it("ignores a superseded response instead of letting it overwrite the fresh one", async () => {
    let resolveStale!: (v: api.MetricsTimeseries) => void;
    vi.mocked(api.getMetricsTimeseries)
      .mockReturnValueOnce(
        new Promise<api.MetricsTimeseries>((r) => {
          resolveStale = r;
        })
      )
      .mockResolvedValue(
        ts([{ period: "2024-02-01", value: 0, heating_seconds: 3600, cooling_seconds: 0 }])
      );

    const { container, rerender } = renderWithUnit(
      <HeatingCoolingHoursChart entityId={entityId} range={range} />
    );
    // Range changes while the first request is still in flight.
    rerender(
      <UnitContext.Provider value={buildUnitContext("F")}>
        <HeatingCoolingHoursChart entityId={entityId} range={laterRange} />
      </UnitContext.Provider>
    );
    await chartLaidOut(container);
    await waitFor(() => expect(tickLabels(container, "xAxis")).toContain("02-01"));

    // The abandoned request finally answers — with data for the OLD range.
    await act(async () => {
      resolveStale(
        ts([{ period: "2023-12-25", value: 0, heating_seconds: 60, cooling_seconds: 0 }])
      );
    });
    expect(tickLabels(container, "xAxis")).toContain("02-01");
    expect(tickLabels(container, "xAxis")).not.toContain("12-25");
  });

  it("keeps the fresh request's loading state when a superseded request fails", async () => {
    let rejectStale!: (e: unknown) => void;
    let resolveFresh!: (v: api.MetricsTimeseries) => void;
    vi.mocked(api.getMetricsTimeseries)
      .mockReturnValueOnce(
        new Promise<api.MetricsTimeseries>((_r, rj) => {
          rejectStale = rj;
        })
      )
      .mockReturnValueOnce(
        new Promise<api.MetricsTimeseries>((r) => {
          resolveFresh = r;
        })
      );

    const { container, rerender } = renderWithUnit(
      <DutyCycleChart entityId={entityId} range={range} />
    );
    rerender(
      <UnitContext.Provider value={buildUnitContext("F")}>
        <DutyCycleChart entityId={entityId} range={laterRange} />
      </UnitContext.Provider>
    );

    await act(async () => {
      rejectStale(new Error("superseded"));
    });
    // The in-flight request owns the loading state; the abandoned one must not
    // end it, or the chart flashes its empty state mid-refresh.
    expect(container.querySelector(".skeleton-bar")).not.toBeNull();
    expect(screen.queryByText(/No data for this range yet/i)).not.toBeInTheDocument();

    await act(async () => {
      resolveFresh(ts([{ period: "2024-02-01", value: 12.5 }], "duty_cycle"));
    });
    await chartLaidOut(container);
    expect(container.querySelector(".skeleton-bar")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Axis + tooltip formatters
// ---------------------------------------------------------------------------

describe("HeatingCoolingHoursChart", () => {
  it("treats missing heating/cooling seconds as zero and labels the axis in hours", async () => {
    vi.mocked(api.getMetricsTimeseries).mockResolvedValue(
      ts([
        // heating_seconds/cooling_seconds absent — the API omits them for days
        // with no run-time. They must plot as 0, never as NaN.
        { period: "2024-01-01", value: 0 },
        { period: "2024-01-02", value: 0, heating_seconds: 7200, cooling_seconds: 3600 },
      ])
    );
    const { container } = renderWithUnit(
      <HeatingCoolingHoursChart entityId={entityId} range={range} />
    );
    await screen.findByText(/Heating & cooling hours per day/i);
    await chartLaidOut(container);
    const ticks = tickLabels(container, "yAxis");
    expect(ticks[0]).toBe("0h");
    expect(ticks).toContain("3h");
    expect(ticks.join(" ")).not.toContain("NaN");
    // 7200s heating + 3600s cooling = 2.00h + 1.00h in the tooltip.
    const text = tooltipText(container, 2);
    expect(text).toContain("2.00h");
    expect(text).toContain("1.00h");
  });
});

describe("AvgCycleDurationChart", () => {
  it("formats the minutes axis and tooltip", async () => {
    vi.mocked(api.getMetricsTimeseries).mockResolvedValue(
      ts([{ period: "2024-01-01", value: 1830 }], "avg_duration")
    );
    const { container } = renderWithUnit(
      <AvgCycleDurationChart entityId={entityId} range={range} />
    );
    await screen.findByText(/Average cycle duration/i);
    await chartLaidOut(container);
    expect(tickLabels(container, "yAxis")).toContain("0m");
    // 1830s ÷ 60 = 30.5 min.
    expect(tooltipText(container)).toContain("30.5 min");
  });
});

describe("TimeToTargetChart", () => {
  it("formats the minutes axis and renders the tooltip via fmtMinutes", async () => {
    vi.mocked(api.getMetricsTimeseries).mockResolvedValue(
      ts([{ period: "2024-01-01", value: 900 }], "time_to_target")
    );
    const { container } = renderWithUnit(<TimeToTargetChart entityId={entityId} range={range} />);
    await screen.findByText(/Time to target/i);
    await chartLaidOut(container);
    expect(tickLabels(container, "yAxis")).toContain("0m");
    // 900s → 15 min on the series; the tooltip re-multiplies by 60 for fmtMinutes.
    expect(tooltipText(container)).toContain("15m");
  });
});

describe("DutyCycleChart", () => {
  it("formats the tooltip as a percentage", async () => {
    vi.mocked(api.getMetricsTimeseries).mockResolvedValue(
      ts([{ period: "2024-01-01", value: 42.25 }], "duty_cycle")
    );
    const { container } = renderWithUnit(<DutyCycleChart entityId={entityId} range={range} />);
    await screen.findByText(/Duty cycle/i);
    await chartLaidOut(container);
    expect(tooltipText(container)).toContain("42.3%");
  });
});

describe("CyclesVsOutsideTempChart", () => {
  it("leaves a null outside temperature un-converted instead of plotting 0", async () => {
    vi.mocked(api.getMetricsCyclesVsOutsideTemp).mockResolvedValue({
      thermostat_entity_id: entityId,
      start: range.start,
      end: range.end,
      points: [
        {
          cycle_id: "c1",
          mode: "heating",
          outside_temp: 50,
          outside_temp_at_end: 51,
          duration_minutes: 40,
          started_at: "2024-01-01T12:00:00",
        },
        // No outside-temp reading for this cycle: the value must stay null,
        // not be fed through the absolute conversion.
        //
        // DEFENSIVE-ONLY (Issue #607): unlike the vent timeline's
        // `cycle_ended_at`, the backend cannot actually produce this —
        // `compute_cycles_vs_outside_temp` filters `outside_temp_at_start IS
        // NOT NULL` (db.py, and `test_metrics_phase2.py` pins it), so
        // `outside_temp: number` in api.ts is correct and must NOT be widened.
        // The cast stays deliberately: it is what keeps the chart's runtime
        // null guard covered against a hand-rolled or replayed payload.
        {
          cycle_id: "c2",
          mode: "heating",
          outside_temp: null as unknown as number,
          outside_temp_at_end: null,
          duration_minutes: 30,
          started_at: "2024-01-02T12:00:00",
        },
      ],
    });
    const { container } = renderWithUnit(
      <CyclesVsOutsideTempChart entityId={entityId} range={range} />,
      "C"
    );
    await screen.findByText(/Cycles vs outside temperature/i);
    await chartLaidOut(container);
    // Only the point with a real reading gets converted (50 °F → 10.0 °C);
    // the null one must NOT become toDisplay(null) === -17.8 °C.
    const text = tooltipText(container, 2);
    expect(text).toContain("10.0");
    expect(text).not.toContain("-17.8");
  });

  it("formats scatter tooltip values to one decimal place", async () => {
    vi.mocked(api.getMetricsCyclesVsOutsideTemp).mockResolvedValue({
      thermostat_entity_id: entityId,
      start: range.start,
      end: range.end,
      points: [
        {
          cycle_id: "c1",
          mode: "heating",
          outside_temp: 41,
          outside_temp_at_end: 42,
          duration_minutes: 33.333,
          started_at: "2024-01-01T12:00:00",
        },
      ],
    });
    const { container } = renderWithUnit(
      <CyclesVsOutsideTempChart entityId={entityId} range={range} />
    );
    await screen.findByText(/Cycles vs outside temperature/i);
    await chartLaidOut(container);
    // Exact segments (with recharts' unit suffix) so an extra decimal place
    // would not slip through a substring match.
    const text = tooltipText(container);
    expect(text).toContain("Duration (min) : 33.3m");
    expect(text).toContain("Outside °F : 41.0°F");
  });
});

describe("CompletionRateChart", () => {
  it("renders 0% rather than NaN% when the payload omits completed_count", () => {
    renderWithUnit(
      <CompletionRateChart
        summary={{
          ...summary,
          completed_count: undefined as unknown as number,
          timeout_count: 3,
          aborted_count: 0,
        }}
        loading={false}
      />
    );
    expect(screen.getByText("3 cycles — 0.0% completed")).toBeInTheDocument();
  });
});

describe("PerRoomHeatingCoolingChart", () => {
  it("labels the hours axis and formats the tooltip to 2dp", async () => {
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
          heating_seconds: 5400,
          cooling_seconds: 1800,
          avg_time_to_target_seconds: 600,
        },
      ],
    });
    const { container } = renderWithUnit(
      <PerRoomHeatingCoolingChart entityId={entityId} range={range} />
    );
    await screen.findByText(/Per-room heating vs cooling/i);
    await chartLaidOut(container);
    expect(tickLabels(container, "xAxis")).toContain("0h");
    const text = tooltipText(container);
    expect(text).toContain("1.50h"); // 5400s heating
    expect(text).toContain("0.50h"); // 1800s cooling
  });
});

describe("RoomParticipationChart", () => {
  it("labels the percent axis and pulls pct + count out of the tooltip payload", async () => {
    vi.mocked(api.getMetricsRoomBreakdown).mockResolvedValue({
      thermostat_entity_id: entityId,
      start: range.start,
      end: range.end,
      rooms: [
        {
          room_id: "r1",
          room_name: "Living Room",
          participation_count: 7,
          participation_rate: 0.7,
          heating_seconds: 0,
          cooling_seconds: 0,
          avg_time_to_target_seconds: null,
        },
      ],
    });
    const { container } = renderWithUnit(
      <RoomParticipationChart entityId={entityId} range={range} />
    );
    await screen.findByText(/Room participation rate/i);
    await chartLaidOut(container);
    expect(tickLabels(container, "xAxis")).toContain("0%");
    expect(tickLabels(container, "xAxis")).toContain("100%");
    expect(tooltipText(container)).toContain("Living RoomParticipation % : 70% (7 cycles)");
  });

  it("falls back to 0 cycles when the payload omits participation_count", async () => {
    vi.mocked(api.getMetricsRoomBreakdown).mockResolvedValue({
      thermostat_entity_id: entityId,
      start: range.start,
      end: range.end,
      rooms: [
        {
          room_id: "r1",
          room_name: "Living Room",
          // participation_count omitted by the backend for this row.
          participation_count: undefined as unknown as number,
          participation_rate: 0.4,
          heating_seconds: 0,
          cooling_seconds: 0,
          avg_time_to_target_seconds: null,
        },
      ],
    });
    const { container } = renderWithUnit(
      <RoomParticipationChart entityId={entityId} range={range} />
    );
    await screen.findByText(/Room participation rate/i);
    await chartLaidOut(container);
    // "undefined cycles" would be the un-defaulted rendering.
    expect(tooltipText(container)).toContain("Living RoomParticipation % : 40% (0 cycles)");
  });
});

describe("DegreeMinutesChart", () => {
  it("labels the tooltip with the active unit and the delta-scaled magnitude", async () => {
    vi.mocked(api.getMetricsTimeseries).mockResolvedValue(
      ts([{ period: "2024-01-01", value: 90 }], "degree_minutes")
    );
    const { container } = renderWithUnit(
      <DegreeMinutesChart entityId={entityId} range={range} />,
      "C"
    );
    await screen.findByText(/Degree-minutes/i);
    await chartLaidOut(container);
    // 90 °F·min is a DELTA: ×5/9 = 50.0 °C·min. The absolute conversion
    // would have produced 32.2, and the label must say °C, not °F.
    const text = tooltipText(container);
    expect(text).toContain("50.0 °C·min");
    expect(text).not.toContain("°F");
  });
});

describe("OvershootHistogramChart", () => {
  it("plots 0 for a bin whose count the backend omitted", async () => {
    vi.mocked(api.getMetricsOvershootHistogram).mockResolvedValue({
      thermostat_entity_id: entityId,
      start_date: range.start,
      end_date: range.end,
      bin_size: 1,
      labels: ["0-1", "1-2", "2-3"],
      // Deliberately short: the third bin has no count.
      counts: [3, 1],
      total_room_cycles: 4,
      overshot_count: 4,
      overshot_pct: 100,
      max_overshoot_f: 2.5,
      avg_overshoot_f: 1.2,
    });
    const { container } = renderWithUnit(
      <OvershootHistogramChart entityId={entityId} range={range} />
    );
    await screen.findByText(/Overshoot histogram/i);
    await chartLaidOut(container);
    expect(tickLabels(container, "xAxis")).toEqual(["0-1", "1-2", "2-3"]);
    expect(tickLabels(container, "yAxis").join(" ")).not.toContain("NaN");
    expect(tooltipText(container, 3)).toContain("2-3Room-cycles : 0");
  });

  // The backend hard-codes °F bin boundaries, so in Celsius mode BOTH the
  // numbers and the suffix have to be rewritten for the x-axis. The subtitle
  // test in MetricsCharts.test.tsx covers `fmtOvershootDelta`, but nothing
  // pinned the axis: with °F-mode labels ("0-1", no suffix) localizeBinLabel is
  // an identity function, so dropping the call entirely left every Fahrenheit
  // assertion green while a °C user read °F boundaries under a °C axis.
  it("localizes the °F bin boundaries onto the x-axis in Celsius mode (#291)", async () => {
    vi.mocked(api.getMetricsOvershootHistogram).mockResolvedValue({
      thermostat_entity_id: entityId,
      start_date: range.start,
      end_date: range.end,
      bin_size: 1,
      labels: ["0–1°F", "2–3°F", "≥5°F"],
      counts: [3, 1, 2],
      total_room_cycles: 6,
      overshot_count: 6,
      overshot_pct: 100,
      max_overshoot_f: 5,
      avg_overshoot_f: 2,
    });
    const { container } = renderWithUnit(
      <OvershootHistogramChart entityId={entityId} range={range} />,
      "C"
    );
    await screen.findByText(/Overshoot histogram/i);
    await chartLaidOut(container);

    // Delta conversion (×5/9, no −32): 1→0.6, 2→1.1, 3→1.7, 5→2.8.
    expect(tickLabels(container, "xAxis")).toEqual(["0.0–0.6°C", "1.1–1.7°C", "≥2.8°C"]);
    // The raw °F labels must be gone entirely — not merely joined by °C ones.
    expect(tickLabels(container, "xAxis").join(" ")).not.toContain("°F");
  });

  it("leaves the °F bin labels untouched in Fahrenheit mode", async () => {
    vi.mocked(api.getMetricsOvershootHistogram).mockResolvedValue({
      thermostat_entity_id: entityId,
      start_date: range.start,
      end_date: range.end,
      bin_size: 1,
      labels: ["0–1°F", "2–3°F", "≥5°F"],
      counts: [3, 1, 2],
      total_room_cycles: 6,
      overshot_count: 6,
      overshot_pct: 100,
      max_overshoot_f: 5,
      avg_overshoot_f: 2,
    });
    const { container } = renderWithUnit(
      <OvershootHistogramChart entityId={entityId} range={range} />
    );
    await screen.findByText(/Overshoot histogram/i);
    await chartLaidOut(container);
    expect(tickLabels(container, "xAxis")).toEqual(["0–1°F", "2–3°F", "≥5°F"]);
  });
});

describe("VentTimelineChart", () => {
  it("colours a cooling event with the cooling colour, not the heating one", async () => {
    vi.mocked(api.getMetricsVentTimeline).mockResolvedValue({
      thermostat_entity_id: entityId,
      start: range.start,
      end: range.end,
      note: "",
      events: [
        {
          cycle_id: "c1",
          timestamp: "2024-01-01T12:00:00",
          entity_id: "cover.a",
          room_id: "r1",
          action: "open",
          reason: "cycle start",
          cycle_mode: "cooling",
          cycle_started_at: "2024-01-01T12:00:00",
          cycle_ended_at: null,
        },
        {
          cycle_id: "c2",
          timestamp: "2024-01-02T12:00:00",
          entity_id: "cover.b",
          room_id: "r1",
          action: "close",
          reason: "cycle end",
          cycle_mode: "heating",
          cycle_started_at: "2024-01-02T12:00:00",
          cycle_ended_at: null,
        },
      ],
    });
    renderWithUnit(<VentTimelineChart entityId={entityId} range={range} />);
    const coolBadge = await screen.findByText("cooling");
    const heatBadge = screen.getByText("heating");
    expect(coolBadge).toHaveStyle({ background: COLORS.cooling });
    expect(heatBadge).toHaveStyle({ background: COLORS.heating });
    expect(COLORS.cooling).not.toBe(COLORS.heating);
  });

  it("omits the disclosure note when the backend sends none", async () => {
    vi.mocked(api.getMetricsVentTimeline).mockResolvedValue({
      thermostat_entity_id: entityId,
      start: range.start,
      end: range.end,
      note: "",
      events: [
        {
          cycle_id: "c1",
          timestamp: "2024-01-01T12:00:00",
          entity_id: "cover.a",
          room_id: "r1",
          action: "open",
          reason: "cycle start",
          cycle_mode: "heating",
          cycle_started_at: "2024-01-01T12:00:00",
          cycle_ended_at: null,
        },
      ],
    });
    const { container } = renderWithUnit(<VentTimelineChart entityId={entityId} range={range} />);
    await screen.findByText("cover.a");
    expect(container.querySelectorAll(".text-sm.text-muted").length).toBe(1); // just the subtitle
  });

  it("renders the running cycle's event, whose cycle_ended_at is null", async () => {
    // Issue #607: the engine writes `opened_at_start` right after
    // `insert_cycle_log`, so a live thermostat's timeline carries events whose
    // parent cycle has no `ended_at` yet — and the endpoint deliberately keeps
    // them. `VentTimelineEvent.cycle_ended_at` is therefore `string | null`;
    // the fixture below assigning a bare `null` (no `as unknown as string`) is
    // itself the type assertion, and the row must still render in full.
    vi.mocked(api.getMetricsVentTimeline).mockResolvedValue({
      thermostat_entity_id: entityId,
      start: range.start,
      end: range.end,
      note: "Cycle-boundary events only",
      events: [
        {
          cycle_id: "running",
          timestamp: "2024-01-03T09:30:00",
          entity_id: "cover.upstairs_office_vent",
          room_id: "room1",
          action: "opened_at_start",
          reason: null,
          cycle_mode: "cooling",
          cycle_started_at: "2024-01-03T09:30:00",
          cycle_ended_at: null,
        },
      ],
    });
    renderWithUnit(<VentTimelineChart entityId={entityId} range={range} />);
    // Every column the table renders is present, so a null `cycle_ended_at`
    // does not blank or break the row.
    expect(await screen.findByText("cover.upstairs_office_vent")).toBeInTheDocument();
    expect(screen.getByText("opened_at_start")).toBeInTheDocument();
    expect(screen.getByText("cooling")).toBeInTheDocument();
    // The "When" cell is derived from `timestamp`, never `cycle_ended_at`, so
    // it is a real date rather than the `Invalid Date` a naive
    // `new Date(e.cycle_ended_at + "Z")` would produce.
    expect(screen.queryByText(/Invalid Date/)).toBeNull();
    expect(screen.getByText(new Date("2024-01-03T09:30:00Z").toLocaleString())).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Eco charts fed a null impact (the page passes null while the fetch is in
// flight and after a failure) — every one of them must degrade to the empty
// state instead of throwing on `impact.days`.
// ---------------------------------------------------------------------------

describe("Eco charts with a null impact", () => {
  it("EcoCyclesPerDayChart renders the empty state", () => {
    renderWithUnit(<EcoCyclesPerDayChart impact={null} loading={false} />);
    expect(screen.getByText(/No data for this range yet/i)).toBeInTheDocument();
  });

  it("EcoDriftPerDayChart renders the empty state", () => {
    renderWithUnit(<EcoDriftPerDayChart impact={null} loading={false} />);
    expect(screen.getByText(/No data for this range yet/i)).toBeInTheDocument();
  });

  it("EcoRoomDriftChart renders its bespoke empty state", () => {
    renderWithUnit(<EcoRoomDriftChart impact={null} loading={false} />);
    expect(screen.getByText(/No Eco-relaxed room-cycles in this range/i)).toBeInTheDocument();
  });
});

describe("Eco drift charts in Celsius", () => {
  it("scales EcoRoomDriftChart bars and axis by the DELTA conversion", async () => {
    const { container } = renderWithUnit(
      <EcoRoomDriftChart impact={ecoImpact} loading={false} />,
      "C"
    );
    await screen.findByText(/Eco drift by room/i);
    await chartLaidOut(container);
    // Axis ticks carry the °C label, and the tooltip reports the avg 2 °F
    // drift as +1.11 °C (delta) — never the absolute −16.67 °C.
    expect(tickLabels(container, "xAxis").join(" ")).toContain("°C");
    const text = tooltipText(container);
    expect(text).toContain("1.11°C");
    expect(text).toContain("2.22°C"); // max drift 4 °F
    expect(text).not.toContain("-16");

    // Pin each value to ITS OWN series. Asserting only that both numbers are
    // somewhere in the tooltip passes just as happily when avg and max are
    // wired to each other's dataKey — and a chart that reports the peak
    // relaxation as the average is exactly the reading an operator would act
    // on. The tooltip renders "<series name> : <value>" per row, so anchor the
    // value directly to the name it follows — swapping the two dataKeys makes
    // both of these fail.
    expect(text).toMatch(/Avg drift\s*:\s*1\.11°C/);
    expect(text).toMatch(/Max drift\s*:\s*2\.22°C/);
  });

  it("uses room_id as the bar label when the room has no name", async () => {
    const { container } = renderWithUnit(
      <EcoRoomDriftChart
        impact={{
          ...ecoImpact,
          rooms: [
            {
              room_id: "room-uuid-2",
              name: null,
              eco_active_cycles: 1,
              avg_drift_f: 1,
              max_drift_f: 1,
            },
          ],
        }}
        loading={false}
      />
    );
    await screen.findByText(/Eco drift by room/i);
    await chartLaidOut(container);
    await waitFor(() => expect(tickLabels(container, "yAxis")).toContain("room-uuid-2"));
  });
});
