import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import Metrics from "./Metrics";
import * as api from "../api";

vi.mock("../api");

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
};

describe("Metrics Page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.getThermostats as any).mockResolvedValue([
      { thermostat_entity_id: "climate.test", name: "Main HVAC" },
    ]);
    (api.getMetricsHomeSummary as any).mockResolvedValue(mockSummary);
    (api.getMetricsThermostatSummary as any).mockResolvedValue({
      ...mockSummary,
      thermostat_entity_id: "climate.test",
    });
    (api.getOutsideTempEntity as any).mockResolvedValue({
      entity_id: "sensor.outside_temp",
      current_value: 42,
    });
    (api.getHAEntities as any).mockResolvedValue([
      { entity_id: "sensor.outside_temp", friendly_name: "Outside Temp" },
    ]);
    (api.setOutsideTempEntity as any).mockResolvedValue({
      entity_id: "sensor.outside_temp",
      current_value: 42,
    });

    // Default mocks for charts to prevent "then" of undefined
    (api.getMetricsTimeseries as any).mockResolvedValue({ series: [] });
    (api.getMetricsCyclesVsOutsideTemp as any).mockResolvedValue({ points: [] });
    (api.getMetricsRoomBreakdown as any).mockResolvedValue({ rooms: [] });
    (api.getMetricsOvershootHistogram as any).mockResolvedValue({
      labels: [],
      counts: [],
      total_room_cycles: 0,
    });
    (api.getMetricsHourHeatmap as any).mockResolvedValue({ grid_seconds: [], day_labels: [] });
    (api.getMetricsVentTimeline as any).mockResolvedValue({ events: [], note: "" });
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

  it("handles outside temp entity change", async () => {
    render(<Metrics />);

    // Find the select by its default option or label context
    const select = await screen.findByDisplayValue(/Outside Temp/i);
    fireEvent.change(select, { target: { value: "sensor.outside_temp" } });

    await waitFor(() => {
      expect(api.setOutsideTempEntity).toHaveBeenCalledWith("sensor.outside_temp");
    });
  });

  it("handles CSV export", async () => {
    (api.downloadMetricsCsv as any).mockResolvedValue(undefined);
    render(<Metrics />);

    const exportBtn = await screen.findByText(/Export CSV/i);
    fireEvent.click(exportBtn);

    expect(api.downloadMetricsCsv).toHaveBeenCalled();
  });
});
