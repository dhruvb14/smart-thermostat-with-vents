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
    vi.mocked(api.getThermostats).mockResolvedValue([
      {
        thermostat_entity_id: "climate.test",
        name: "Main HVAC",
        default_temp: 72,
        min_setpoint: 60,
        max_setpoint: 80,
        deadband: 0.5,
        max_vent_closed_min: 60,
        min_open_vents: 1,
        overshoot_delta: 0.5,
        cycle_timeout_hours: 2,
        reconciliation_interval_min: 5,
        vacation_hvac_mode: "single" as const,
        min_cycle_runtime_min: 0,
        min_cycle_offtime_min: 0,
      },
    ]);
    vi.mocked(api.getMetricsHomeSummary).mockResolvedValue(mockSummary);
    vi.mocked(api.getMetricsThermostatSummary).mockResolvedValue({
      ...mockSummary,
      thermostat_entity_id: "climate.test",
    });
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

  it("handles outside temp entity change", async () => {
    render(<Metrics />);

    const searchInput = await screen.findByPlaceholderText(/Search sensor \/ weather entities/i);
    fireEvent.focus(searchInput);
    fireEvent.change(searchInput, { target: { value: "outside" } });

    const option = await screen.findByText("Outside Temp");
    fireEvent.mouseDown(option);

    await waitFor(() => {
      expect(api.setOutsideTempEntity).toHaveBeenCalledWith("sensor.outside_temp");
    });
  });

  it("handles CSV export", async () => {
    vi.mocked(api.downloadMetricsCsv).mockReturnValue(undefined);
    render(<Metrics />);

    const exportBtn = await screen.findByText(/Export CSV/i);
    fireEvent.click(exportBtn);

    expect(api.downloadMetricsCsv).toHaveBeenCalled();
  });
});
