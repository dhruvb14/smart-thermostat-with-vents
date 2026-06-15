import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
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

const mockCycleDetail: api.CycleDetail = {
  cycle: mockCycleLogs[0],
  rooms: [
    {
      room_id: "r1",
      name: "Living Room",
      source: "schedule",
      target_temp: 72,
      reached_at: "2024-01-01T12:30:00",
      vent_closed_at: "2024-01-01T12:45:00",
      temp_at_start: 78,
      temp_at_end: 72,
      trigger_detail: { source: "schedule", start_time: "12:00:00", end_time: "13:00:00" },
      joined_at: "2024-01-01T12:05:00",
    },
  ],
  vent_events: [
    {
      id: 1,
      timestamp: "2024-01-01T12:01:00",
      entity_id: "cover.living",
      room_id: "r1",
      action: "open",
      reason: "cycle start",
    },
  ],
  setpoint_history: [{ id: 1, timestamp: "2024-01-01T12:00:00", setpoint: 72, reason: "schedule" }],
};

const mockTempSamples: api.CycleTempSample[] = [
  {
    id: 1,
    cycle_id: "c1",
    room_id: "r1",
    timestamp: "2024-01-01T12:00:00",
    room_temp: 78,
    thermostat_temp: 77,
    setpoint: 72,
  },
  {
    id: 2,
    cycle_id: "c1",
    room_id: "r1",
    timestamp: "2024-01-01T12:30:00",
    room_temp: 72,
    thermostat_temp: 73,
    setpoint: 72,
  },
];

describe("Logs Page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getLogs).mockResolvedValue(mockCycleLogs);
    vi.mocked(api.getEventLogs).mockResolvedValue(mockEventLogs);
    vi.mocked(api.getLogRetention).mockResolvedValue(mockRetention);
    vi.mocked(api.connectWS).mockReturnValue(() => {});
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
    vi.mocked(api.setLogRetention).mockResolvedValue(mockRetention);
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
    vi.mocked(api.clearEventLogs).mockResolvedValue({ cleared: true });
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

  it("cancelling the clear modal does not clear logs", async () => {
    render(<Logs />);
    expect(await screen.findByText(/System started/i)).toBeInTheDocument();

    fireEvent.click(screen.getByText(/Clear logs/i));
    const cancelBtn = await screen.findByRole("button", { name: "Cancel" });
    fireEvent.click(cancelBtn);

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Clear all logs" })).not.toBeInTheDocument();
    });
    expect(api.clearEventLogs).not.toHaveBeenCalled();
  });

  it("does not duplicate rows when loading older entries overlaps the window (Issue #302)", async () => {
    // newest-first rows from `hi` down to `lo`, each with a unique message.
    const makeRows = (hi: number, lo: number): api.EventLogEntry[] =>
      Array.from({ length: hi - lo + 1 }, (_, i) => {
        const id = hi - i;
        return {
          id,
          timestamp: "2024-01-01T12:00:00",
          message: `evt-${id}`,
          level: "info" as const,
          category: "system",
          details: null,
        };
      });

    // Initial load returns a full page (ids 100..51). The "Load older" fetch
    // returns ids 51..2 — overlapping id 51 (as happens once a new head row
    // has slid the offset window).
    vi.mocked(api.getEventLogs)
      .mockResolvedValueOnce(makeRows(100, 51))
      .mockResolvedValueOnce(makeRows(51, 2));

    render(<Logs />);
    expect(await screen.findByText("evt-51")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Load older entries/i }));
    // The genuinely-older rows arrive.
    expect(await screen.findByText("evt-2")).toBeInTheDocument();

    // The overlapping row must appear exactly once, not duplicated.
    expect(screen.getAllByText("evt-51")).toHaveLength(1);
  });

  it("does not duplicate cycles when Load more overlaps the window (Issue #302)", async () => {
    const makeCycles = (hi: number, lo: number): api.CycleLog[] =>
      Array.from({ length: hi - lo + 1 }, (_, i) => {
        const n = hi - i;
        return {
          id: `c${String(n).padStart(7, "0")}`, // 8 chars → unique slice(0,8)
          thermostat_entity_id: "climate.test",
          started_at: "2024-01-01T12:00:00",
          ended_at: "2024-01-01T13:00:00",
          mode: "cool",
          rooms: {},
        };
      });
    vi.mocked(api.getLogs).mockReset();
    vi.mocked(api.getLogs)
      .mockResolvedValueOnce(makeCycles(100, 51))
      .mockResolvedValue(makeCycles(51, 2)); // overlap at c0000051

    render(<Logs />);
    fireEvent.click(screen.getByText("Cycle History"));
    expect(await screen.findByText("c0000100…")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Load more/i }));
    expect(await screen.findByText("c0000002…")).toBeInTheDocument();

    expect(screen.getAllByText("c0000051…")).toHaveLength(1);
  });

  it("filters the live feed by category and toggles levels", async () => {
    render(<Logs />);
    expect(await screen.findByText(/System started/i)).toBeInTheDocument();

    // Change category select → triggers a reload with category param
    const select = screen.getByRole("combobox");
    fireEvent.change(select, { target: { value: "engine" } });
    await waitFor(() => {
      expect(api.getEventLogs).toHaveBeenCalledWith(
        expect.objectContaining({ category: "engine" })
      );
    });

    // Toggle the "info" level off → triggers a reload with a reduced level set
    fireEvent.click(screen.getByRole("button", { name: "info" }));
    await waitFor(() => {
      expect(api.getEventLogs).toHaveBeenCalledWith(
        expect.objectContaining({ levels: expect.not.arrayContaining(["info"]) })
      );
    });
  });

  it("pauses and resumes the live feed", async () => {
    render(<Logs />);
    expect(await screen.findByText(/System started/i)).toBeInTheDocument();

    const pauseBtn = screen.getByRole("button", { name: /Pause/i });
    fireEvent.click(pauseBtn);
    expect(screen.getByRole("button", { name: /Resume/i })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Resume/i }));
    expect(screen.getByRole("button", { name: /Pause/i })).toBeInTheDocument();
  });

  it("appends matching events received over the websocket", async () => {
    let wsHandler: (e: api.WSEvent) => void = () => {};
    vi.mocked(api.connectWS).mockImplementation((cb) => {
      wsHandler = cb;
      return () => {};
    });
    render(<Logs />);
    expect(await screen.findByText(/System started/i)).toBeInTheDocument();

    act(() => {
      wsHandler({
        type: "log_event",
        data: {
          id: 99,
          timestamp: "2024-01-01T12:05:00",
          message: "Live websocket event",
          level: "info",
          category: "system",
          details: null,
        } as unknown as Record<string, unknown>,
      });
    });

    expect(await screen.findByText(/Live websocket event/i)).toBeInTheDocument();
  });

  it("expands an event entry with details", async () => {
    vi.mocked(api.getEventLogs).mockResolvedValue([
      {
        id: 2,
        timestamp: "2024-01-01T12:00:00",
        message: "Detailed event",
        level: "warning",
        category: "engine",
        details: { foo: "bar" },
      },
    ]);
    render(<Logs />);

    const entry = await screen.findByText(/Detailed event/i);
    fireEvent.click(entry);
    expect(await screen.findByText(/"foo": "bar"/)).toBeInTheDocument();
  });

  it("loads cycle detail when a cycle row is expanded", async () => {
    vi.mocked(api.getCycleDetail).mockResolvedValue(mockCycleDetail);
    render(<Logs />);

    fireEvent.click(screen.getByText("Cycle History"));
    const row = await screen.findByText(/climate\.test/i);
    fireEvent.click(row);

    await waitFor(() => {
      expect(api.getCycleDetail).toHaveBeenCalledWith("c1");
    });
    // Detail sections render
    expect(await screen.findByText(/Vent activity/i)).toBeInTheDocument();
    expect(screen.getByText(/Setpoint history/i)).toBeInTheDocument();
    expect(screen.getByText("cover.living")).toBeInTheDocument();
  });

  it("opens the temperature chart modal for a room", async () => {
    vi.mocked(api.getCycleDetail).mockResolvedValue(mockCycleDetail);
    vi.mocked(api.getCycleTempSamples).mockResolvedValue(mockTempSamples);
    render(<Logs />);

    fireEvent.click(screen.getByText("Cycle History"));
    fireEvent.click(await screen.findByText(/climate\.test/i));

    const chartBtn = await screen.findByRole("button", { name: "View chart" });
    fireEvent.click(chartBtn);

    await waitFor(() => {
      expect(api.getCycleTempSamples).toHaveBeenCalledWith("c1", "r1");
    });
    expect(await screen.findByText(/Temperature — Living Room/i)).toBeInTheDocument();
    expect(screen.getByText(/Room avg/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => {
      expect(screen.queryByText(/Temperature — Living Room/i)).not.toBeInTheDocument();
    });
  });

  it("shows an empty state when the temp chart has no samples", async () => {
    vi.mocked(api.getCycleDetail).mockResolvedValue(mockCycleDetail);
    vi.mocked(api.getCycleTempSamples).mockResolvedValue([]);
    render(<Logs />);

    fireEvent.click(screen.getByText("Cycle History"));
    fireEvent.click(await screen.findByText(/climate\.test/i));
    fireEvent.click(await screen.findByRole("button", { name: "View chart" }));

    expect(await screen.findByText(/No temperature samples recorded/i)).toBeInTheDocument();
  });

  it("switches the cycle-history time window to a preset", async () => {
    render(<Logs />);
    fireEvent.click(screen.getByText("Cycle History"));
    await screen.findByText(/climate\.test/i);

    vi.mocked(api.getLogs).mockClear();
    fireEvent.click(screen.getByRole("button", { name: "7d" }));

    await waitFor(() => {
      expect(api.getLogs).toHaveBeenCalledWith(
        expect.objectContaining({ since: expect.any(String) })
      );
    });
  });

  it("reveals custom date inputs when the custom window is selected", async () => {
    render(<Logs />);
    fireEvent.click(screen.getByText("Cycle History"));
    await screen.findByText(/climate\.test/i);

    fireEvent.click(screen.getByRole("button", { name: "Custom" }));
    const dateInputs = document.querySelectorAll('input[type="datetime-local"]');
    expect(dateInputs.length).toBe(2);

    // Typing in the inputs should NOT trigger a fetch — Apply gates it
    vi.mocked(api.getLogs).mockClear();
    fireEvent.change(dateInputs[0], { target: { value: "2024-01-01T00:00" } });
    fireEvent.change(dateInputs[1], { target: { value: "2024-01-02T00:00" } });
    expect(api.getLogs).not.toHaveBeenCalled();

    // Clicking Apply commits the range and fires exactly one fetch
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => {
      expect(api.getLogs).toHaveBeenCalledWith(
        expect.objectContaining({ since: expect.any(String), until: expect.any(String) })
      );
    });
  });

  it("paginates cycle history with Load more", async () => {
    const page = Array.from({ length: 50 }, (_, i) => ({
      ...mockCycleLogs[0],
      id: `c${i}`,
      thermostat_entity_id: `climate.t${i}`,
    }));
    const secondPage = [{ ...mockCycleLogs[0], id: "c-extra" }];
    vi.mocked(api.getLogs).mockResolvedValueOnce(page).mockResolvedValueOnce(secondPage);
    render(<Logs />);

    fireEvent.click(screen.getByText("Cycle History"));
    const loadMore = await screen.findByRole("button", { name: /Load more/i });
    fireEvent.click(loadMore);

    await waitFor(() => {
      expect(api.getLogs).toHaveBeenCalledWith(expect.objectContaining({ offset: 50 }));
    });
  });

  it("shows an empty state when there are no cycle logs", async () => {
    vi.mocked(api.getLogs).mockResolvedValue([]);
    render(<Logs />);
    fireEvent.click(screen.getByText("Cycle History"));
    expect(await screen.findByText(/No cycle logs in this time window/i)).toBeInTheDocument();
  });

  it("flags overflow cycles and renders the overflow rooms section (Issue #254)", async () => {
    vi.mocked(api.getLogs).mockResolvedValue([{ ...mockCycleLogs[0], had_overflow: true }]);
    vi.mocked(api.getCycleDetail).mockResolvedValue({
      ...mockCycleDetail,
      cycle: { ...mockCycleLogs[0], had_overflow: true },
      rooms: [
        { ...mockCycleDetail.rooms[0], role: "active" },
        {
          room_id: "r_office",
          name: "Office",
          source: null,
          target_temp: 70,
          reached_at: null,
          vent_closed_at: "2024-01-01T12:50:00",
          temp_at_start: 75,
          temp_at_end: 71,
          trigger_detail: { overflow: true, tier: 1 },
          joined_at: "2024-01-01T12:40:00",
          role: "overflow",
        },
      ],
    });
    render(<Logs />);

    fireEvent.click(screen.getByText("Cycle History"));
    // The list row carries the overflow badge.
    expect(await screen.findAllByText(/overflow/i)).not.toHaveLength(0);

    fireEvent.click(await screen.findByText(/climate\.test/i));

    // The dedicated overflow section renders with the redirected room + tier.
    expect(await screen.findByText(/Overflow rooms/i)).toBeInTheDocument();
    expect(screen.getByText(/min-runtime redirect/i)).toBeInTheDocument();
    expect(screen.getByText("Office")).toBeInTheDocument();
    expect(screen.getByText(/tier 1/i)).toBeInTheDocument();
  });

  it("omits the overflow section for cycles without overflow rooms", async () => {
    vi.mocked(api.getCycleDetail).mockResolvedValue(mockCycleDetail);
    render(<Logs />);

    fireEvent.click(screen.getByText("Cycle History"));
    fireEvent.click(await screen.findByText(/climate\.test/i));

    expect(await screen.findByText(/Vent activity/i)).toBeInTheDocument();
    expect(screen.queryByText(/Overflow rooms/i)).not.toBeInTheDocument();
  });

  it("surfaces an error when saving retention fails", async () => {
    vi.mocked(api.setLogRetention).mockRejectedValue(new Error("Save boom"));
    render(<Logs />);

    fireEvent.click(screen.getByText("Retention"));
    await screen.findByDisplayValue("7");
    fireEvent.click(screen.getByText("Save"));

    expect(await screen.findByText(/Save boom/i)).toBeInTheDocument();
  });

  it("loads older live-feed entries with Load older entries", async () => {
    const page = Array.from({ length: 50 }, (_, i) => ({
      ...mockEventLogs[0],
      id: i + 1,
      message: `Event ${i}`,
    }));
    const secondPage = [{ ...mockEventLogs[0], id: 999, message: "Older event" }];
    vi.mocked(api.getEventLogs).mockResolvedValueOnce(page).mockResolvedValueOnce(secondPage);
    render(<Logs />);

    const loadOlder = await screen.findByRole("button", { name: /Load older entries/i });
    fireEvent.click(loadOlder);

    await waitFor(() => {
      expect(api.getEventLogs).toHaveBeenCalledWith(expect.objectContaining({ offset: 50 }));
    });
  });
});
