import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act, within } from "@testing-library/react";
import Logs from "./Logs";
import * as api from "../api";

vi.mock("../api");

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const retention: api.LogRetentionSettings = {
  event_log_retention_days: 7,
  cycle_log_retention_days: 30,
};

function evt(over: Partial<api.EventLogEntry> = {}): api.EventLogEntry {
  return {
    id: 1,
    timestamp: "2024-01-01T12:00:00",
    level: "info",
    category: "system",
    message: "System started",
    details: null,
    ...over,
  };
}

function cycle(over: Partial<api.CycleLog> = {}): api.CycleLog {
  return {
    id: "c1000000",
    thermostat_entity_id: "climate.test",
    started_at: "2024-01-01T12:00:00",
    ended_at: "2024-01-01T13:00:00",
    ended_reason: "completed",
    mode: "cool",
    rooms: {},
    ...over,
  };
}

function room(over: Partial<api.CycleRoomDetail> = {}): api.CycleRoomDetail {
  return {
    room_id: "r1abcdef01",
    name: "Living Room",
    source: "schedule",
    target_temp: 72,
    reached_at: "2024-01-01T12:30:00",
    vent_closed_at: "2024-01-01T12:45:00",
    temp_at_start: 78,
    temp_at_end: 72,
    trigger_detail: { source: "schedule", start_time: "12:00:00", end_time: "13:00:00" },
    joined_at: "2024-01-01T12:05:00",
    ...over,
  };
}

function detailOf(
  rooms: api.CycleRoomDetail[],
  over: Partial<api.CycleDetail> = {}
): api.CycleDetail {
  return {
    cycle: cycle(),
    rooms,
    vent_events: [],
    setpoint_history: [],
    ...over,
  };
}

/** A promise plus its resolve/reject handles, for ordering-sensitive tests. */
function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Open Cycle History and expand the single cycle row. */
async function expandFirstCycle() {
  fireEvent.click(screen.getByRole("button", { name: "Cycle History" }));
  fireEvent.click(await screen.findByText("climate.test"));
}

describe("Logs — coverage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getLogs).mockResolvedValue([cycle()]);
    vi.mocked(api.getEventLogs).mockResolvedValue([evt()]);
    vi.mocked(api.getLogRetention).mockResolvedValue(retention);
    vi.mocked(api.getCycleDetail).mockResolvedValue(detailOf([room()]));
    vi.mocked(api.getCycleTempSamples).mockResolvedValue([]);
    vi.mocked(api.connectWS).mockReturnValue(() => {});
  });

  // -------------------------------------------------------------------------
  // Cycle History — row rendering (duration / outcome / mode)
  // -------------------------------------------------------------------------

  it("renders a still-running cycle as Active with a running duration", async () => {
    vi.mocked(api.getLogs).mockResolvedValue([
      cycle({ ended_at: null, ended_reason: null, mode: "cooling" }),
    ]);
    render(<Logs />);
    fireEvent.click(screen.getByRole("button", { name: "Cycle History" }));

    const row = (await screen.findByText("climate.test")).closest("tr")!;
    expect(within(row).getByText("Active")).toBeInTheDocument();
    // duration() short-circuits on a null end.
    expect(within(row).getByText("running")).toBeInTheDocument();
    // Ended cell falls back to the em-dash placeholder.
    expect(row.querySelector('td[data-label="Ended"]')).toHaveTextContent("—");
    // mode "cooling" picks the blue badge (heating/anything else is orange).
    expect(within(row).getByText("cooling")).toHaveClass("badge-blue");

    // Expanded: "Ended reason" falls through null ended_reason to "running".
    fireEvent.click(screen.getByText("climate.test"));
    const card = (await screen.findByText("Ended reason")).closest("div.card")!;
    expect(card).toHaveTextContent("running");
  });

  it("renders each ended-cycle outcome and a sub-hour duration", async () => {
    vi.mocked(api.getLogs).mockResolvedValue([
      cycle({ id: "cA000000", ended_reason: "completed", ended_at: "2024-01-01T12:05:00" }),
      cycle({ id: "cB000000", ended_reason: "timeout" }),
      cycle({ id: "cC000000", ended_reason: "aborted: opposite cycle" }),
      cycle({ id: "cD000000", ended_reason: "" }),
    ]);
    render(<Logs />);
    fireEvent.click(screen.getByRole("button", { name: "Cycle History" }));

    expect(await screen.findByText("Completed")).toHaveClass("badge-blue");
    expect(screen.getByText("Timeout")).toHaveClass("badge-orange");
    // "aborted:<reason>" is rewritten for display and coloured red.
    expect(screen.getByText("Aborted: opposite cycle")).toHaveClass("badge-red");
    // An unknown/blank reason degrades to a generic grey "Ended".
    expect(screen.getByText("Ended", { selector: "span.badge" })).toHaveClass("badge-gray");

    // 12:00 → 12:05 is under an hour, so minutes (not "0.1h").
    expect(screen.getByText("5m")).toBeInTheDocument();
  });

  it("refreshes the cycle list from the Refresh button", async () => {
    render(<Logs />);
    fireEvent.click(screen.getByRole("button", { name: "Cycle History" }));
    await screen.findByText("climate.test");

    vi.mocked(api.getLogs).mockClear();
    vi.mocked(api.getLogs).mockResolvedValue([
      cycle({ thermostat_entity_id: "climate.refetched" }),
    ]);
    fireEvent.click(screen.getByRole("button", { name: /Refresh/ }));

    expect(await screen.findByText("climate.refetched")).toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Cycle History — expanded detail
  // -------------------------------------------------------------------------

  it("summarises every trigger source in the expanded room table", async () => {
    vi.mocked(api.getCycleDetail).mockResolvedValue(
      detailOf([
        room({
          room_id: "rovr00001",
          name: "Override room",
          source: "override",
          trigger_detail: { source: "override", expires_at: "2024-01-01T14:00:00" },
        }),
        room({
          room_id: "rovr00002",
          name: "Override forever",
          source: "override",
          trigger_detail: { source: "override" },
        }),
        room({
          room_id: "rpre00001",
          name: "Presence holdover",
          source: "presence",
          trigger_detail: { source: "presence", holdover_expires_at: "2024-01-01T14:30:00" },
        }),
        room({
          room_id: "rpre00002",
          name: "Presence bare",
          source: "presence",
          trigger_detail: { source: "presence" },
        }),
        room({
          room_id: "rsch00002",
          name: "Schedule no times",
          source: "schedule",
          trigger_detail: { source: "schedule" },
        }),
        room({
          room_id: "rukn00001",
          name: "Unknown source",
          source: "manual",
          trigger_detail: { source: "manual" },
        }),
      ])
    );
    render(<Logs />);
    await expandFirstCycle();

    expect(await screen.findByText(/^override, expires /)).toBeInTheDocument();
    expect(screen.getByText(/^presence, holdover ends /)).toBeInTheDocument();
    // Without an expiry the summary is the bare source word. "override" also
    // appears in that room's Trigger cell, so scope the lookup to the summary
    // line (the muted div), of which there is exactly one per bare room.
    const bareOverride = screen.getByText("Override forever").closest("tr")!;
    expect(within(bareOverride).getByText("override", { selector: "div" })).toBeInTheDocument();
    const barePresence = screen.getByText("Presence bare").closest("tr")!;
    expect(within(barePresence).getByText("presence", { selector: "div" })).toBeInTheDocument();
    const bareSchedule = screen.getByText("Schedule no times").closest("tr")!;
    expect(within(bareSchedule).getByText("schedule", { selector: "div" })).toBeInTheDocument();
    // An unrecognised source is echoed verbatim rather than dropped.
    const unknown = screen.getByText("Unknown source").closest("tr")!;
    expect(within(unknown).getByText("manual", { selector: "div" })).toBeInTheDocument();
  });

  it("falls back to a truncated room id and dashes when a room is unnamed", async () => {
    const onlyRoom = room({
      room_id: "r1abcdef01",
      name: null,
      source: null,
      reached_at: null,
      vent_closed_at: null,
      trigger_detail: { tier: 2 }, // no `source` key → empty summary
    });
    vi.mocked(api.getCycleDetail).mockResolvedValue(detailOf([onlyRoom]));
    vi.mocked(api.getCycleTempSamples).mockResolvedValue([]);
    render(<Logs />);
    await expandFirstCycle();

    const row = (await screen.findByText("r1abcdef")).closest("tr")!;
    // Trigger cell: no source → em-dash, and triggerSummary() yields "".
    expect(row.querySelector('td[data-label="Trigger"]')).toHaveTextContent("—");
    // fmtTime(null) for both timestamps.
    expect(row.querySelector('td[data-label="Reached"]')).toHaveTextContent("—");
    expect(row.querySelector('td[data-label="Vent closed"]')).toHaveTextContent("—");

    // The chart modal title reuses the same id fallback for its heading.
    fireEvent.click(within(row).getByRole("button", { name: "View chart" }));
    expect(await screen.findByText(/Temperature — r1abcdef/)).toBeInTheDocument();
  });

  it("renders an overflow room with no tier and no name", async () => {
    vi.mocked(api.getLogs).mockResolvedValue([cycle({ had_overflow: true })]);
    vi.mocked(api.getCycleDetail).mockResolvedValue(
      detailOf([
        room({ role: "active" }),
        room({
          room_id: "rovf00001",
          name: null,
          role: "overflow",
          trigger_detail: { overflow: true }, // no tier
          joined_at: null,
          vent_closed_at: null,
        }),
      ])
    );
    render(<Logs />);
    await expandFirstCycle();

    const overflowRow = (await screen.findByText("rovf0000")).closest("tr")!;
    expect(overflowRow.querySelector('td[data-label="Tier"]')).toHaveTextContent("—");
    expect(overflowRow.querySelector('td[data-label="Opened"]')).toHaveTextContent("—");

    // The overflow table's own "View chart" button opens the modal with the
    // id fallback as the room name.
    fireEvent.click(within(overflowRow).getByRole("button", { name: "View chart" }));
    expect(await screen.findByText(/Temperature — rovf0000/)).toBeInTheDocument();
  });

  it("falls back to the vent snapshot counts while the detail is still loading", async () => {
    vi.mocked(api.getLogs).mockResolvedValue([
      cycle({
        vents_at_start: { "cover.a": "open", "cover.b": "closed" },
        vents_at_end: {},
      }),
    ]);
    // Never resolves: the row stays in the "detail not loaded" state.
    vi.mocked(api.getCycleDetail).mockReturnValue(new Promise(() => {}));
    render(<Logs />);
    await expandFirstCycle();

    expect(await screen.findByText(/Loading detail/)).toBeInTheDocument();
    expect(screen.getByText(/Vents at start: 2/)).toHaveTextContent("Vents at end: 0");
  });

  // -------------------------------------------------------------------------
  // Temperature chart modal
  // -------------------------------------------------------------------------

  it("shows the fetch error when temperature samples fail to load", async () => {
    vi.mocked(api.getCycleTempSamples).mockRejectedValue(new Error("samples exploded"));
    render(<Logs />);
    await expandFirstCycle();
    fireEvent.click(await screen.findByRole("button", { name: "View chart" }));

    expect(await screen.findByText("samples exploded")).toBeInTheDocument();
    // The error replaces the loading state rather than sitting alongside it.
    expect(screen.queryByText(/Loading samples/)).not.toBeInTheDocument();
  });

  it("shows a generic message when the sample fetch rejects with a non-Error", async () => {
    vi.mocked(api.getCycleTempSamples).mockRejectedValue("nope");
    render(<Logs />);
    await expandFirstCycle();
    fireEvent.click(await screen.findByRole("button", { name: "View chart" }));

    expect(await screen.findByText("Failed to load")).toBeInTheDocument();
  });

  it("draws no chart when every sample value is null", async () => {
    vi.mocked(api.getCycleTempSamples).mockResolvedValue([
      {
        id: 1,
        cycle_id: "c1000000",
        room_id: "r1abcdef01",
        timestamp: "2024-01-01T12:00:00",
        room_temp: null,
        thermostat_temp: null,
        setpoint: null,
      },
    ]);
    render(<Logs />);
    await expandFirstCycle();
    fireEvent.click(await screen.findByRole("button", { name: "View chart" }));

    expect(await screen.findByText(/Temperature — Living Room/)).toBeInTheDocument();
    // Samples exist (so no empty state) but nothing plottable → no axes/legend.
    expect(screen.queryByText(/No temperature samples recorded/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Room avg/)).not.toBeInTheDocument();
  });

  it("closes the chart modal when the backdrop itself is clicked", async () => {
    vi.mocked(api.getCycleTempSamples).mockResolvedValue([]);
    render(<Logs />);
    await expandFirstCycle();
    fireEvent.click(await screen.findByRole("button", { name: "View chart" }));

    const title = await screen.findByText(/Temperature — Living Room/);
    const modal = title.closest(".modal")!;
    // A click that bubbles from inside the dialog must NOT dismiss it.
    fireEvent.click(modal);
    expect(screen.getByText(/Temperature — Living Room/)).toBeInTheDocument();

    // A click on the backdrop itself does.
    fireEvent.click(document.querySelector(".modal-backdrop")!);
    await waitFor(() => {
      expect(screen.queryByText(/Temperature — Living Room/)).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Live Feed
  // -------------------------------------------------------------------------

  it("renders an unrecognised level with the neutral colour and no crash", async () => {
    vi.mocked(api.getEventLogs).mockResolvedValue([
      evt({ level: "debug" as unknown as api.EventLogEntry["level"], message: "Odd level" }),
    ]);
    render(<Logs />);

    const entry = (await screen.findByText("Odd level")).closest(".event-entry")!;
    const level = entry.querySelector<HTMLElement>(".event-level")!;
    expect(level).toHaveTextContent("DEBUG");
    expect(level.style.color).toBe("var(--gray-600)");
  });

  it("renders entries that carry no id", async () => {
    vi.mocked(api.getEventLogs).mockResolvedValue([
      evt({ id: undefined as unknown as number, message: "Idless event" }),
    ]);
    render(<Logs />);
    expect(await screen.findByText("Idless event")).toBeInTheDocument();
  });

  it("commits a custom live-feed window only when Apply is pressed", async () => {
    render(<Logs />);
    await screen.findByText("System started");

    vi.mocked(api.getEventLogs).mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Custom" }));

    // Switching to Custom with nothing committed yet drops both bounds.
    await waitFor(() => {
      expect(api.getEventLogs).toHaveBeenCalledWith(
        expect.objectContaining({ since: undefined, until: undefined })
      );
    });

    const inputs = document.querySelectorAll<HTMLInputElement>('input[type="datetime-local"]');
    expect(inputs).toHaveLength(2);
    vi.mocked(api.getEventLogs).mockClear();
    fireEvent.change(inputs[0], { target: { value: "2025-06-01T00:00" } });
    fireEvent.change(inputs[1], { target: { value: "2025-06-08T00:00" } });
    // Typing alone must not refetch — Apply is the commit point.
    expect(api.getEventLogs).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => {
      expect(api.getEventLogs).toHaveBeenCalledWith(
        expect.objectContaining({
          since: new Date("2025-06-01T00:00").toISOString(),
          until: new Date("2025-06-08T00:00").toISOString(),
        })
      );
    });
  });

  it("ignores a websocket event that the active category filter excludes", async () => {
    let ws: api.WSHandler = () => {};
    vi.mocked(api.connectWS).mockImplementation((cb) => {
      ws = cb;
      return () => {};
    });
    render(<Logs />);
    await screen.findByText("System started");

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "engine" } });
    await waitFor(() => {
      expect(api.getEventLogs).toHaveBeenCalledWith(
        expect.objectContaining({ category: "engine" })
      );
    });

    act(() => {
      ws({
        type: "log_event",
        data: evt({ id: 50, category: "system", message: "Wrong category" }) as unknown as Record<
          string,
          unknown
        >,
      });
      ws({
        type: "log_event",
        data: evt({ id: 51, category: "engine", message: "Right category" }) as unknown as Record<
          string,
          unknown
        >,
      });
    });

    expect(await screen.findByText("Right category")).toBeInTheDocument();
    expect(screen.queryByText("Wrong category")).not.toBeInTheDocument();
  });

  it("ignores websocket frames that are not log events", async () => {
    let ws: api.WSHandler = () => {};
    vi.mocked(api.connectWS).mockImplementation((cb) => {
      ws = cb;
      return () => {};
    });
    render(<Logs />);
    await screen.findByText("System started");
    expect(screen.getByText("1 events")).toBeInTheDocument();

    act(() => {
      ws({
        type: "zone_status",
        data: evt({ id: 60, message: "Not an event log row" }) as unknown as Record<
          string,
          unknown
        >,
      });
    });

    expect(screen.queryByText("Not an event log row")).not.toBeInTheDocument();
    expect(screen.getByText("1 events")).toBeInTheDocument();
  });

  it("does not re-append a websocket event whose id is already displayed (#302)", async () => {
    let ws: api.WSHandler = () => {};
    vi.mocked(api.connectWS).mockImplementation((cb) => {
      ws = cb;
      return () => {};
    });
    vi.mocked(api.getEventLogs).mockResolvedValue([evt({ id: 7, message: "Raced event" })]);
    render(<Logs />);
    await screen.findByText("Raced event");

    act(() => {
      ws({
        type: "log_event",
        data: evt({ id: 7, message: "Raced event" }) as unknown as Record<string, unknown>,
      });
    });

    expect(screen.getAllByText("Raced event")).toHaveLength(1);
    expect(screen.getByText("1 events")).toBeInTheDocument();
  });

  it("re-enables a level that was toggled off", async () => {
    render(<Logs />);
    await screen.findByText("System started");

    fireEvent.click(screen.getByRole("button", { name: "warning" }));
    await waitFor(() => {
      expect(api.getEventLogs).toHaveBeenCalledWith(
        expect.objectContaining({ levels: ["info", "error"] })
      );
    });

    // Toggling it back on restores the full set, which the API call omits.
    fireEvent.click(screen.getByRole("button", { name: "warning" }));
    await waitFor(() => {
      expect(api.getEventLogs).toHaveBeenLastCalledWith(
        expect.objectContaining({ levels: undefined })
      );
    });
    expect(screen.getByRole("button", { name: "warning" })).toHaveClass("btn-primary");
  });

  it("discards a superseded live-feed response (#302)", async () => {
    const slow = deferred<api.EventLogEntry[]>();
    const fast = deferred<api.EventLogEntry[]>();
    vi.mocked(api.getEventLogs)
      .mockResolvedValueOnce([evt({ id: 1, message: "initial" })])
      .mockReturnValueOnce(slow.promise)
      .mockReturnValueOnce(fast.promise);

    render(<Logs />);
    await screen.findByText("initial");

    // Two rapid filter switches: the first (slow) response lands last.
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "engine" } });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "api" } });

    await act(async () => {
      fast.resolve([evt({ id: 2, message: "fresh api rows" })]);
      await fast.promise;
    });
    expect(await screen.findByText("fresh api rows")).toBeInTheDocument();

    await act(async () => {
      slow.resolve([evt({ id: 3, message: "stale engine rows" })]);
      await slow.promise;
    });

    // The stale response must not overwrite the fresher one.
    expect(screen.queryByText("stale engine rows")).not.toBeInTheDocument();
    expect(screen.getByText("fresh api rows")).toBeInTheDocument();
  });

  it("returns to the Live Feed tab", async () => {
    render(<Logs />);
    await screen.findByText("System started");

    fireEvent.click(screen.getByRole("button", { name: "Retention" }));
    await screen.findByDisplayValue("7");
    expect(screen.queryByText("System started")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Live Feed" }));
    expect(await screen.findByText("System started")).toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Retention settings — the #600 mount-fetch loading gate
  // -------------------------------------------------------------------------

  it("renders no retention input until the mount fetch resolves (#600)", async () => {
    const d = deferred<api.LogRetentionSettings>();
    vi.mocked(api.getLogRetention).mockReturnValue(d.promise);
    render(<Logs />);

    fireEvent.click(screen.getByRole("button", { name: "Retention" }));
    // The gate: while in flight there is nothing to type into, so a late
    // resolve cannot overwrite an operator's keystrokes.
    expect(await screen.findByText(/Loading settings/)).toBeInTheDocument();
    expect(document.querySelectorAll('input[type="number"]')).toHaveLength(0);

    await act(async () => {
      d.resolve({ event_log_retention_days: 14, cycle_log_retention_days: 60 });
      await d.promise;
    });

    // The operator's configured values render — never the seeded defaults.
    expect(await screen.findByDisplayValue("14")).toBeInTheDocument();
    expect(screen.getByDisplayValue("60")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("7")).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("30")).not.toBeInTheDocument();
  });

  it("clears the gate and shows the seeded defaults when the retention fetch fails", async () => {
    vi.mocked(api.getLogRetention).mockRejectedValue(new Error("offline"));
    render(<Logs />);
    fireEvent.click(screen.getByRole("button", { name: "Retention" }));

    // The form must still become usable rather than spinning forever…
    expect(await screen.findByDisplayValue("7")).toBeInTheDocument();
    expect(screen.getByDisplayValue("30")).toBeInTheDocument();
    expect(screen.queryByText(/Loading settings/)).not.toBeInTheDocument();
    // …and Save is live, so these shown values are what a blind Save would
    // POST. See the note in this PR: the failed fetch is not surfaced.
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("clamps both retention inputs to a minimum of one day", async () => {
    vi.mocked(api.setLogRetention).mockResolvedValue(retention);
    render(<Logs />);
    fireEvent.click(screen.getByRole("button", { name: "Retention" }));

    const eventInput = await screen.findByDisplayValue("7");
    const cycleInput = screen.getByDisplayValue("30");
    // A cleared field parses to NaN; the || 1 fallback keeps a valid number.
    fireEvent.change(eventInput, { target: { value: "" } });
    fireEvent.change(cycleInput, { target: { value: "" } });
    expect((eventInput as HTMLInputElement).value).toBe("1");
    expect((cycleInput as HTMLInputElement).value).toBe("1");

    // A parsable but out-of-range value is clamped by Math.max instead.
    fireEvent.change(cycleInput, { target: { value: "-5" } });
    expect((cycleInput as HTMLInputElement).value).toBe("1");

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(api.setLogRetention).toHaveBeenCalledWith({
        event_log_retention_days: 1,
        cycle_log_retention_days: 1,
      });
    });
  });

  it("edits the cycle-history retention independently of the event retention", async () => {
    vi.mocked(api.setLogRetention).mockResolvedValue(retention);
    render(<Logs />);
    fireEvent.click(screen.getByRole("button", { name: "Retention" }));

    const cycleInput = await screen.findByDisplayValue("30");
    fireEvent.change(cycleInput, { target: { value: "45" } });

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(api.setLogRetention).toHaveBeenCalledWith({
        event_log_retention_days: 7,
        cycle_log_retention_days: 45,
      });
    });
  });

  it("shows a generic message when the retention save rejects with a non-Error", async () => {
    vi.mocked(api.setLogRetention).mockRejectedValue("boom");
    render(<Logs />);
    fireEvent.click(screen.getByRole("button", { name: "Retention" }));
    await screen.findByDisplayValue("7");

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Save failed")).toBeInTheDocument();
  });

  describe("saved confirmation timer", () => {
    beforeEach(() => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
    });
    afterEach(() => {
      vi.useRealTimers();
    });

    it("shows Saved! and clears it, restarting the timer on a second save", async () => {
      vi.mocked(api.setLogRetention).mockResolvedValue({
        event_log_retention_days: 9,
        cycle_log_retention_days: 30,
      });
      render(<Logs />);
      fireEvent.click(screen.getByRole("button", { name: "Retention" }));
      await screen.findByDisplayValue("7");

      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      expect(await screen.findByText("Saved!")).toBeInTheDocument();
      // The server's echoed value replaces the local form state.
      expect(screen.getByDisplayValue("9")).toBeInTheDocument();

      // Saving again while the badge is up clears the pending timer and
      // starts a fresh one, so the badge stays visible.
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      await waitFor(() => expect(api.setLogRetention).toHaveBeenCalledTimes(2));
      expect(screen.getByText("Saved!")).toBeInTheDocument();

      act(() => {
        vi.advanceTimersByTime(2100);
      });
      await waitFor(() => expect(screen.queryByText("Saved!")).not.toBeInTheDocument());
    });
  });
});
