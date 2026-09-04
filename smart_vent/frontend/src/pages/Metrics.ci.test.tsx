import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
// The `api` binding below is a runtime value from a dynamic import (needed for
// the fresh module registry), so it cannot also serve as a type namespace.
import type * as ApiTypes from "../api";

// The Metrics page pins its date range to the seeded demo-data week under CI
// (Issue #442) so the charts and the two <input type="date"> controls render
// identical pixels on the golden-screenshot update pass and verify pass.
// Without the pin, `defaultRange()` bakes "today" into the goldens and the
// visual-regression legs can never stabilise across a date boundary.
//
// The pin lives behind `ciPinned`, whose `isCI` flag is a module-level constant
// derived from VITE_APP_VERSION — so, exactly as Dashboard.ci.test.tsx and
// ci.test.tsx do, stub the env and re-import with a fresh module registry.
// The ordinary Metrics specs run with isCI false and therefore cannot see this
// branch at all: they assert whatever "today" happens to be.

vi.mock("../api");

vi.mock("recharts", async () => {
  const actual = await vi.importActual<typeof import("recharts")>("recharts");
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
      <div style={{ width: 800, height: 300 }}>{children}</div>
    ),
  };
});

// This spec asserts the pinned date range, not chart contents. The chart
// endpoints have six differing envelope shapes (some arrays, some objects with a
// `rooms`/`points`/`series` key); an empty payload keeps every chart inert, so
// declare one inert value rather than hand-building six fixtures nothing reads.
const EMPTY = [] as never;

const mockSummary = {
  start_date: "2025-06-01",
  end_date: "2025-06-07",
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

const mockEcoImpact = {
  start_date: "2025-06-01",
  end_date: "2025-06-07",
  thermostat_entity_id: null,
  total_cycles: 10,
  total_seconds: 10800,
  eco_active_cycles: 2,
  eco_active_seconds: 3000,
  avg_drift_f: 2.5,
  days: [],
  rooms: [],
};

describe("Metrics — CI build (#442)", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  const renderUnderCI = async () => {
    vi.resetModules();
    vi.stubEnv("VITE_APP_VERSION", "CI");

    const api = await import("../api");
    vi.mocked(api.getThermostats).mockResolvedValue([]);
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: "sensor.outdoor",
      current_value: 60,
    });
    vi.mocked(api.getMetricsHomeSummary).mockResolvedValue(
      mockSummary as unknown as ApiTypes.MetricsSummary
    );
    vi.mocked(api.getMetricsEcoImpact).mockResolvedValue(
      mockEcoImpact as unknown as ApiTypes.EcoImpact
    );
    vi.mocked(api.getMetricsTimeseries).mockResolvedValue(EMPTY);
    vi.mocked(api.getMetricsRoomBreakdown).mockResolvedValue(EMPTY);
    vi.mocked(api.getMetricsCyclesVsOutsideTemp).mockResolvedValue(EMPTY);
    vi.mocked(api.getMetricsOvershootHistogram).mockResolvedValue(EMPTY);
    vi.mocked(api.getMetricsHourHeatmap).mockResolvedValue(EMPTY);
    vi.mocked(api.getMetricsVentTimeline).mockResolvedValue(EMPTY);

    const { CI_METRICS_RANGE } = await import("../ci");
    const Metrics = (await import("./Metrics")).default;
    render(<Metrics />);
    return { api, CI_METRICS_RANGE };
  };

  it("pins the date pickers to the seeded demo week rather than to today", async () => {
    const { CI_METRICS_RANGE } = await renderUnderCI();

    // The two date inputs are the surface that would otherwise bake the run
    // date into the golden PNG.
    await waitFor(() => {
      const dates = document.querySelectorAll<HTMLInputElement>('input[type="date"]');
      expect(dates).toHaveLength(2);
      expect(dates[0].value).toBe(CI_METRICS_RANGE.start);
      expect(dates[1].value).toBe(CI_METRICS_RANGE.end);
    });

    // Guard against the pin being satisfied by coincidence: the seeded window
    // is a fixed week in the past, so it can never equal today's rolling range.
    const today = new Date();
    const yyyyMmDd = [
      today.getFullYear(),
      String(today.getMonth() + 1).padStart(2, "0"),
      String(today.getDate()).padStart(2, "0"),
    ].join("-");
    expect(CI_METRICS_RANGE.end).not.toBe(yyyyMmDd);
  });

  it("fetches the summary for the pinned window, so charts read seeded rows only", async () => {
    const { api, CI_METRICS_RANGE } = await renderUnderCI();

    await waitFor(() => expect(api.getMetricsHomeSummary).toHaveBeenCalled());
    expect(api.getMetricsHomeSummary).toHaveBeenCalledWith({
      start: CI_METRICS_RANGE.start,
      end: CI_METRICS_RANGE.end,
    });

    // …and the range is echoed in the page subtitle, which is itself captured
    // in the golden.
    expect(
      await screen.findByText(
        new RegExp(`${CI_METRICS_RANGE.start}\\s*→\\s*${CI_METRICS_RANGE.end}`)
      )
    ).toBeInTheDocument();
  });
});
