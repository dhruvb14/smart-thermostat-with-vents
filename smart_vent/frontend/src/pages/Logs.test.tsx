import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import Logs from "./Logs";
import * as api from "../api";

vi.mock("../api");

const mockRetention = {
  event_log_retention_days: 7,
  cycle_log_retention_days: 30,
};

const mockEventLogs: api.EventLogEntry[] = [
  {
    id: 1,
    timestamp: "2024-01-01T12:00:00",
    message: "System started",
    level: "info",
    category: "system",
    details: null,
  },
];

const mockCycleLogs: api.CycleLog[] = [
  {
    id: "c1",
    thermostat_entity_id: "climate.test",
    started_at: "2024-01-01T12:00:00",
    ended_at: "2024-01-01T13:00:00",
    mode: "cool",
    rooms: { r1: { name: "Living Room", target: 72, source: "schedule" } },
  },
];

describe("Logs Page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.getLogs as any).mockResolvedValue(mockCycleLogs);
    (api.getEventLogs as any).mockResolvedValue(mockEventLogs);
    (api.getLogRetention as any).mockResolvedValue(mockRetention);
    (api.connectWS as any).mockReturnValue(() => {});
  });

  it("renders the logs page and switches tabs", async () => {
    render(<Logs />);

    // Default tab is Live Feed
    expect(await screen.findByText(/System started/i)).toBeInTheDocument();

    // Switch to Cycle History
    fireEvent.click(screen.getByText("Cycle History"));
    expect(await screen.findByText(/climate\.test/i)).toBeInTheDocument();

    // Switch to Retention
    fireEvent.click(screen.getByText("Retention"));
    const retentionTitles = await screen.findAllByText(/Log Retention/i);
    expect(retentionTitles[0]).toBeInTheDocument();
  });

  it("updates retention settings", async () => {
    (api.setLogRetention as any).mockResolvedValue(mockRetention);
    render(<Logs />);

    fireEvent.click(screen.getByText("Retention"));

    const eventInput = await screen.findByDisplayValue("7");
    fireEvent.change(eventInput, { target: { value: "14" } });

    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => {
      expect(api.setLogRetention).toHaveBeenCalledWith(
        expect.objectContaining({
          event_log_retention_days: 14,
        })
      );
    });
  });

  it("handles clearing event logs", async () => {
    (api.clearEventLogs as any).mockResolvedValue({ cleared: true });
    render(<Logs />);

    expect(await screen.findByText(/System started/i)).toBeInTheDocument();

    const clearBtn = screen.getByText(/Clear logs/i);
    fireEvent.click(clearBtn);

    // Confirmation modal
    const confirmBtn = await screen.findByRole("button", { name: "Clear all logs" });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(api.clearEventLogs).toHaveBeenCalled();
    });
  });
});
