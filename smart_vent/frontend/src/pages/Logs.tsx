import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useUnit } from "../contexts";
import { ciPinned, CI_LOGS_RANGE } from "../ci";
import ConfirmDialog from "../components/ConfirmDialog";
import {
  getLogs,
  getEventLogs,
  clearEventLogs,
  getLogRetention,
  setLogRetention,
  getCycleDetail,
  getCycleTempSamples,
  connectWS,
  type CycleLog,
  type CycleDetail,
  type CycleTempSample,
  type EventLogEntry,
  type LogRetentionSettings,
} from "../api";

// ---------------------------------------------------------------------------
// Shared: time-window helpers
// ---------------------------------------------------------------------------

type TimePreset = "1h" | "6h" | "24h" | "7d" | "custom";

const PRESETS: { label: string; value: TimePreset }[] = [
  { label: "1h", value: "1h" },
  { label: "6h", value: "6h" },
  { label: "24h", value: "24h" },
  { label: "7d", value: "7d" },
  { label: "Custom", value: "custom" },
];

function presetToSince(preset: TimePreset): string | undefined {
  if (preset === "custom") return undefined;
  const ms: Record<string, number> = { "1h": 1, "6h": 6, "24h": 24, "7d": 168 };
  const d = new Date();
  d.setHours(d.getHours() - ms[preset]);
  return d.toISOString();
}

// ---------------------------------------------------------------------------
// Shared: time-window controls
// ---------------------------------------------------------------------------

function TimeWindowControls({
  preset,
  customFrom,
  customTo,
  onPreset,
  onCustomFrom,
  onCustomTo,
  onApply,
}: {
  preset: TimePreset;
  customFrom: string;
  customTo: string;
  onPreset: (p: TimePreset) => void;
  onCustomFrom: (v: string) => void;
  onCustomTo: (v: string) => void;
  onApply?: () => void;
}) {
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: ".4rem", alignItems: "center" }}>
      <span className="text-sm text-muted" style={{ marginRight: ".25rem" }}>
        Window:
      </span>
      {PRESETS.map((p) => (
        <button
          key={p.value}
          className={`btn btn-sm ${preset === p.value ? "btn-primary" : "btn-secondary"}`}
          onClick={() => onPreset(p.value)}
        >
          {p.label}
        </button>
      ))}
      {preset === "custom" && (
        <>
          <input
            type="datetime-local"
            className="form-control form-control-sm"
            style={{ width: "auto" }}
            value={customFrom}
            onChange={(e) => onCustomFrom(e.target.value)}
          />
          <span className="text-sm text-muted">to</span>
          <input
            type="datetime-local"
            className="form-control form-control-sm"
            style={{ width: "auto" }}
            value={customTo}
            onChange={(e) => onCustomTo(e.target.value)}
          />
          <button
            className="btn btn-sm btn-primary"
            onClick={onApply}
            disabled={!customFrom || !customTo}
          >
            Apply
          </button>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Cycle History tab
// ---------------------------------------------------------------------------

function duration(start: string, end: string | null): string {
  if (!end) return "running";
  const ms = new Date(end).getTime() - new Date(start).getTime();
  const mins = Math.round(ms / 60000);
  if (mins < 60) return `${mins}m`;
  return `${(mins / 60).toFixed(1)}h`;
}

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso + "Z").toLocaleTimeString();
}

type OutcomeKind = "active" | "completed" | "timeout" | "aborted" | "unknown";

function outcomeOf(log: CycleLog): { kind: OutcomeKind; label: string; color: string } {
  if (!log.ended_at) return { kind: "active", label: "Active", color: "green" };
  const reason = log.ended_reason ?? "";
  if (reason === "completed") return { kind: "completed", label: "Completed", color: "blue" };
  if (reason === "timeout") return { kind: "timeout", label: "Timeout", color: "orange" };
  if (reason.startsWith("aborted"))
    return {
      kind: "aborted",
      label: reason.replace(/^aborted:\s*/, "Aborted: "),
      color: "red",
    };
  return { kind: "unknown", label: reason || "Ended", color: "gray" };
}

function triggerSummary(detail: Record<string, unknown> | null): string {
  if (!detail) return "";
  const source = detail.source as string | undefined;
  if (source === "schedule") {
    const start = detail.start_time as string | undefined;
    const end = detail.end_time as string | undefined;
    if (start && end) return `schedule ${start.slice(0, 5)}–${end.slice(0, 5)}`;
    return "schedule";
  }
  if (source === "override") {
    const exp = detail.expires_at as string | undefined;
    return exp ? `override, expires ${new Date(exp + "Z").toLocaleString()}` : "override";
  }
  if (source === "presence") {
    const exp = detail.holdover_expires_at as string | undefined;
    return exp ? `presence, holdover ends ${new Date(exp + "Z").toLocaleString()}` : "presence";
  }
  if (source === "safety") {
    return "safety protection";
  }
  return source ?? "";
}

function TempChartModal({
  cycleId,
  roomId,
  roomName,
  onClose,
}: {
  cycleId: string;
  roomId: string;
  roomName: string;
  onClose: () => void;
}) {
  const { fmtTemp } = useUnit();
  const [samples, setSamples] = useState<CycleTempSample[] | null>(null);
  const [err, setErr] = useState<string>("");

  useEffect(() => {
    getCycleTempSamples(cycleId, roomId)
      .then(setSamples)
      .catch((e) => setErr(e instanceof Error ? e.message : "Failed to load"));
  }, [cycleId, roomId]);

  const svg = useMemo(() => {
    if (!samples || samples.length === 0) return null;
    const W = 720;
    const H = 320;
    const PAD = 40;
    const t0 = new Date(samples[0].timestamp + "Z").getTime();
    const tN = new Date(samples[samples.length - 1].timestamp + "Z").getTime();
    const dt = Math.max(1, tN - t0);
    const values: number[] = [];
    for (const s of samples) {
      if (s.room_temp != null) values.push(s.room_temp);
      if (s.thermostat_temp != null) values.push(s.thermostat_temp);
      if (s.setpoint != null) values.push(s.setpoint);
    }
    if (values.length === 0) return null;
    const minV = Math.min(...values) - 1;
    const maxV = Math.max(...values) + 1;
    const dv = Math.max(0.01, maxV - minV);
    const x = (ts: string) => PAD + ((new Date(ts + "Z").getTime() - t0) / dt) * (W - 2 * PAD);
    const y = (v: number) => H - PAD - ((v - minV) / dv) * (H - 2 * PAD);

    const series = (key: "room_temp" | "thermostat_temp" | "setpoint") =>
      samples
        .filter((s) => s[key] != null)
        .map((s) => `${x(s.timestamp).toFixed(1)},${y(s[key] as number).toFixed(1)}`)
        .join(" ");

    const roomPts = series("room_temp");
    const thermoPts = series("thermostat_temp");
    const setpointPts = series("setpoint");

    return { W, H, PAD, roomPts, thermoPts, setpointPts, minV, maxV };
  }, [samples]);

  return createPortal(
    <div className="modal-backdrop" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" style={{ width: "min(800px, 95vw)" }}>
        <div className="modal-title">Temperature — {roomName}</div>
        {err && <div className="badge badge-red">{err}</div>}
        {!err && samples === null && (
          <div className="loading">
            <div className="spinner" /> Loading samples…
          </div>
        )}
        {samples && samples.length === 0 && (
          <div className="empty-state">
            <p>No temperature samples recorded for this room in this cycle.</p>
          </div>
        )}
        {svg && (
          <>
            <svg
              width="100%"
              viewBox={`0 0 ${svg.W} ${svg.H}`}
              style={{ background: "var(--gray-50)", borderRadius: 4 }}
            >
              <line
                x1={svg.PAD}
                y1={svg.H - svg.PAD}
                x2={svg.W - svg.PAD}
                y2={svg.H - svg.PAD}
                stroke="var(--gray-400)"
              />
              <line
                x1={svg.PAD}
                y1={svg.PAD}
                x2={svg.PAD}
                y2={svg.H - svg.PAD}
                stroke="var(--gray-400)"
              />
              <text x={svg.PAD} y={svg.PAD - 8} fontSize="10" fill="var(--gray-600)">
                {fmtTemp(svg.maxV)}
              </text>
              <text x={svg.PAD} y={svg.H - svg.PAD + 16} fontSize="10" fill="var(--gray-600)">
                {fmtTemp(svg.minV)}
              </text>
              {svg.roomPts && (
                <polyline fill="none" stroke="#2563eb" strokeWidth="2" points={svg.roomPts} />
              )}
              {svg.thermoPts && (
                <polyline
                  fill="none"
                  stroke="#f97316"
                  strokeWidth="2"
                  strokeDasharray="4 3"
                  points={svg.thermoPts}
                />
              )}
              {svg.setpointPts && (
                <polyline
                  fill="none"
                  stroke="#6b7280"
                  strokeWidth="1.5"
                  strokeDasharray="2 3"
                  points={svg.setpointPts}
                />
              )}
            </svg>
            <div style={{ display: "flex", gap: "1rem", marginTop: ".5rem", fontSize: ".8rem" }}>
              <span style={{ color: "#2563eb" }}>■ Room avg</span>
              <span style={{ color: "#f97316" }}>■ Thermostat</span>
              <span style={{ color: "#6b7280" }}>■ Setpoint</span>
            </div>
          </>
        )}
        <div className="modal-footer">
          <button className="btn btn-secondary" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
}

function LogRow({ log }: { log: CycleLog }) {
  const [expanded, setExpanded] = useState(false);
  const [detail, setDetail] = useState<CycleDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [chartRoom, setChartRoom] = useState<{ id: string; name: string } | null>(null);
  const outcome = outcomeOf(log);

  useEffect(() => {
    if (expanded && !detail && !loadingDetail) {
      setLoadingDetail(true);
      getCycleDetail(log.id)
        .then(setDetail)
        .finally(() => setLoadingDetail(false));
    }
  }, [expanded, detail, loadingDetail, log.id]);

  const rooms = Object.values(log.rooms);

  return (
    <>
      <tr style={{ cursor: "pointer" }} onClick={() => setExpanded((e) => !e)}>
        <td className="font-mono" data-label="ID" style={{ fontSize: ".75rem" }}>
          {log.id.slice(0, 8)}…
        </td>
        <td className="font-mono" data-label="Thermostat" style={{ fontSize: ".8rem" }}>
          {log.thermostat_entity_id}
        </td>
        <td data-label="Mode">
          <span className={`badge badge-${log.mode === "cooling" ? "blue" : "orange"}`}>
            {log.mode}
          </span>
        </td>
        <td data-label="Outcome">
          <span className={`badge badge-${outcome.color}`}>{outcome.label}</span>
          {log.had_overflow && (
            <span
              className="badge badge-purple"
              title="Redirected surplus air into non-active rooms during a minimum-runtime hold"
              style={{ marginLeft: ".35rem" }}
            >
              overflow
            </span>
          )}
          {log.eco_active && (
            <span
              className="badge badge-green"
              title="Eco Mode relaxed at least one room's target based on the outdoor temperature"
              style={{ marginLeft: ".35rem" }}
            >
              Eco Mode
            </span>
          )}
        </td>
        <td data-label="Started">{new Date(log.started_at + "Z").toLocaleString()}</td>
        <td data-label="Ended">
          {log.ended_at ? (
            new Date(log.ended_at + "Z").toLocaleString()
          ) : (
            <span className="text-sm text-muted">—</span>
          )}
        </td>
        <td data-label="Duration">{duration(log.started_at, log.ended_at)}</td>
        <td data-label="Rooms">{rooms.length}</td>
        <td data-label="Details">{expanded ? "▲" : "▼"}</td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={9} style={{ background: "var(--gray-50)", padding: ".75rem 1rem" }}>
            <CycleExpanded
              log={log}
              detail={detail}
              loading={loadingDetail}
              onShowChart={(id, name) => setChartRoom({ id, name })}
            />
          </td>
        </tr>
      )}
      {chartRoom && (
        <TempChartModal
          cycleId={log.id}
          roomId={chartRoom.id}
          roomName={chartRoom.name}
          onClose={() => setChartRoom(null)}
        />
      )}
    </>
  );
}

function CycleExpanded({
  log,
  detail,
  loading,
  onShowChart,
}: {
  log: CycleLog;
  detail: CycleDetail | null;
  loading: boolean;
  onShowChart: (roomId: string, roomName: string) => void;
}) {
  const { fmtTemp } = useUnit();
  const fmt = (v: number | null | undefined) => (v != null ? fmtTemp(v) : "—");
  const ventStart = log.vents_at_start ?? {};
  const ventEnd = log.vents_at_end ?? {};

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: ".75rem" }}>
      {/* Zone summary */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))",
          gap: ".5rem",
        }}
      >
        <div className="card" style={{ padding: ".6rem .75rem" }}>
          <div className="text-sm text-muted">Thermostat temp</div>
          <div style={{ fontWeight: 600 }}>
            {fmt(log.thermostat_temp_at_start)} → {fmt(log.thermostat_temp_at_end)}
          </div>
        </div>
        <div
          className="card"
          style={{ padding: ".6rem .75rem" }}
          title="Setpoint commanded to the thermostat (always a whole degree) at cycle start → end"
        >
          <div className="text-sm text-muted">Setpoint</div>
          <div style={{ fontWeight: 600 }}>
            {fmt(log.setpoint_at_start)} → {fmt(log.setpoint_at_end)}
          </div>
        </div>
        <div
          className="card"
          style={{ padding: ".6rem .75rem" }}
          title="Outdoor temperature at cycle start → end — the input Eco Mode uses to relax room targets"
        >
          <div className="text-sm text-muted">Outside temp{log.eco_active ? " 🌿" : ""}</div>
          <div style={{ fontWeight: 600 }}>
            {fmt(log.outside_temp_at_start)} → {fmt(log.outside_temp_at_end)}
          </div>
        </div>
        <div className="card" style={{ padding: ".6rem .75rem" }}>
          <div className="text-sm text-muted">Ended reason</div>
          <div style={{ fontWeight: 600 }}>
            {log.ended_reason ?? (log.ended_at ? "—" : "running")}
          </div>
        </div>
      </div>

      {loading && (
        <div className="loading">
          <div className="spinner" /> Loading detail…
        </div>
      )}

      {/* Rooms table */}
      {detail && (
        <div className="card" style={{ padding: 0 }}>
          <div
            style={{
              padding: ".5rem .75rem",
              fontWeight: 600,
              borderBottom: "1px solid var(--gray-200)",
            }}
          >
            Rooms
          </div>
          <div className="table-wrap">
            <table className="table-cards">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Trigger</th>
                  <th>Target</th>
                  <th>Start → End</th>
                  <th>Reached</th>
                  <th>Vent closed</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {detail.rooms
                  .filter((r) => r.role !== "overflow")
                  .map((r) => (
                    <tr key={r.room_id}>
                      <td data-label="Name" style={{ fontWeight: 500 }}>
                        {r.name ?? r.room_id.slice(0, 8)}
                      </td>
                      <td data-label="Trigger" className="td-stack">
                        <span className="text-sm">{r.source ?? "—"}</span>
                        {r.trigger_detail && (
                          <div className="text-sm text-muted">
                            {triggerSummary(r.trigger_detail)}
                          </div>
                        )}
                        {r.joined_at && (
                          <div className="text-sm text-muted">
                            joined {new Date(r.joined_at + "Z").toLocaleTimeString()}
                          </div>
                        )}
                      </td>
                      <td data-label="Target">
                        {r.eco_active && r.requested_target != null ? (
                          // Eco Mode relaxed this room (Issue #404): show the
                          // requested ask and the relaxed target it ran to.
                          <span title="Eco Mode relaxed this room's target">
                            {fmtTemp(r.requested_target)} → 🌿 {fmtTemp(r.target_temp)}
                          </span>
                        ) : (
                          fmtTemp(r.target_temp)
                        )}
                      </td>
                      <td data-label="Start → End">
                        {fmt(r.temp_at_start)} → {fmt(r.temp_at_end)}
                      </td>
                      <td data-label="Reached">{fmtTime(r.reached_at)}</td>
                      <td data-label="Vent closed">{fmtTime(r.vent_closed_at)}</td>
                      <td>
                        <button
                          className="btn btn-sm btn-secondary"
                          onClick={() => onShowChart(r.room_id, r.name ?? r.room_id.slice(0, 8))}
                        >
                          View chart
                        </button>
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Overflow rooms (Issue #254) — non-active rooms the cycle redirected
          surplus conditioned air into during its minimum-runtime hold. */}
      {detail && detail.rooms.some((r) => r.role === "overflow") && (
        <div className="card" style={{ padding: 0 }}>
          <div
            style={{
              padding: ".5rem .75rem",
              fontWeight: 600,
              borderBottom: "1px solid var(--gray-200)",
              display: "flex",
              alignItems: "center",
              gap: ".5rem",
            }}
          >
            Overflow rooms
            <span className="badge badge-purple">min-runtime redirect</span>
          </div>
          <div className="table-wrap">
            <table className="table-cards">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Tier</th>
                  <th>Setpoint</th>
                  <th>Start → End</th>
                  <th>Opened</th>
                  <th>Closed</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {detail.rooms
                  .filter((r) => r.role === "overflow")
                  .map((r) => {
                    const tier = (r.trigger_detail?.tier as number | undefined) ?? null;
                    return (
                      <tr key={r.room_id}>
                        <td data-label="Name" style={{ fontWeight: 500 }}>
                          {r.name ?? r.room_id.slice(0, 8)}
                        </td>
                        <td data-label="Tier">
                          {tier != null ? (
                            <span className="badge badge-gray">tier {tier}</span>
                          ) : (
                            "—"
                          )}
                        </td>
                        <td data-label="Setpoint">{fmtTemp(r.target_temp)}</td>
                        <td data-label="Start → End">
                          {fmt(r.temp_at_start)} → {fmt(r.temp_at_end)}
                        </td>
                        <td data-label="Opened">{fmtTime(r.joined_at)}</td>
                        <td data-label="Closed">{fmtTime(r.vent_closed_at)}</td>
                        <td>
                          <button
                            className="btn btn-sm btn-secondary"
                            onClick={() => onShowChart(r.room_id, r.name ?? r.room_id.slice(0, 8))}
                          >
                            View chart
                          </button>
                        </td>
                      </tr>
                    );
                  })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Vent activity */}
      {detail && detail.vent_events.length > 0 && (
        <div className="card" style={{ padding: 0 }}>
          <div
            style={{
              padding: ".5rem .75rem",
              fontWeight: 600,
              borderBottom: "1px solid var(--gray-200)",
            }}
          >
            Vent activity ({detail.vent_events.length})
          </div>
          <div
            style={{
              maxHeight: 200,
              overflowY: "auto",
              padding: ".5rem .75rem",
              fontSize: ".8rem",
            }}
          >
            {detail.vent_events.map((ev) => (
              <div key={ev.id} style={{ display: "flex", flexWrap: "wrap", gap: ".75rem" }}>
                <span className="font-mono text-muted">{fmtTime(ev.timestamp)}</span>
                <span style={{ minWidth: "min(160px, 45vw)" }} className="font-mono">
                  {ev.entity_id}
                </span>
                <span className="badge badge-gray">{ev.action}</span>
                {ev.reason && <span className="text-muted">{ev.reason}</span>}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Setpoint history */}
      {detail && detail.setpoint_history.length > 0 && (
        <div className="card" style={{ padding: 0 }}>
          <div
            style={{
              padding: ".5rem .75rem",
              fontWeight: 600,
              borderBottom: "1px solid var(--gray-200)",
            }}
          >
            Setpoint history ({detail.setpoint_history.length})
          </div>
          <div
            style={{
              maxHeight: 160,
              overflowY: "auto",
              padding: ".5rem .75rem",
              fontSize: ".8rem",
            }}
          >
            {detail.setpoint_history.map((sp) => (
              <div key={sp.id} style={{ display: "flex", flexWrap: "wrap", gap: ".75rem" }}>
                <span className="font-mono text-muted">{fmtTime(sp.timestamp)}</span>
                <span style={{ fontWeight: 600 }}>{fmtTemp(sp.setpoint)}</span>
                {sp.reason && <span className="text-muted">{sp.reason}</span>}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Fallback: vent start/end maps if detail not yet loaded but snapshot present */}
      {!detail && (Object.keys(ventStart).length > 0 || Object.keys(ventEnd).length > 0) && (
        <div className="text-sm text-muted">
          Vents at start: {Object.keys(ventStart).length} • Vents at end:{" "}
          {Object.keys(ventEnd).length}
        </div>
      )}
    </div>
  );
}

const PAGE_SIZE = 50;

// Offset pagination can re-fetch rows that are already displayed once new rows
// are inserted at the head (a new event/cycle, locally via WS or in the DB),
// because the offset window slides. Merge by `id` so an overlapping page never
// produces visible duplicates (or React duplicate-key warnings). (Issue #302)
function mergeUniqueById<T extends { id?: string | number }>(
  incoming: readonly T[],
  existing: readonly T[],
  where: "front" | "back"
): T[] {
  const seen = new Set(existing.map((x) => x.id).filter((id) => id != null));
  const fresh = incoming.filter((x) => x.id == null || !seen.has(x.id));
  return where === "front" ? [...fresh, ...existing] : [...existing, ...fresh];
}

function CycleHistory() {
  const [logs, setLogs] = useState<CycleLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [hasMore, setHasMore] = useState(false);
  const [offset, setOffset] = useState(0);

  // Under CI the window is pinned to the seeded demo week (the Metrics-page
  // pattern, Issue #442): the table then renders REAL rows — the seeded
  // demo- cycles — in the goldens, while live engine cycles (dated "now")
  // fall outside the window and cannot perturb a pixel.
  const [preset, setPreset] = useState<TimePreset>(ciPinned("24h", "custom"));
  const [customFrom, setCustomFrom] = useState(ciPinned("", CI_LOGS_RANGE.from));
  const [customTo, setCustomTo] = useState(ciPinned("", CI_LOGS_RANGE.to));
  const [committedFrom, setCommittedFrom] = useState(ciPinned("", CI_LOGS_RANGE.from));
  const [committedTo, setCommittedTo] = useState(ciPinned("", CI_LOGS_RANGE.to));

  const buildParams = (currentOffset: number) => {
    const since =
      preset !== "custom"
        ? presetToSince(preset)
        : committedFrom
          ? new Date(committedFrom).toISOString()
          : undefined;
    const until =
      preset === "custom" && committedTo ? new Date(committedTo).toISOString() : undefined;
    return { limit: PAGE_SIZE, offset: currentOffset, since, until };
  };

  const loadSeqRef = useRef(0);

  const load = async (reset = false) => {
    const seq = ++loadSeqRef.current;
    setLoading(true);
    const nextOffset = reset ? 0 : offset;
    const rows = await getLogs(buildParams(nextOffset));
    // Ignore a response that a newer load (e.g. a rapid filter switch) has
    // superseded, so a slow stale fetch can't overwrite fresher data. (Issue #302)
    if (seq !== loadSeqRef.current) return;
    if (reset) {
      setLogs(rows);
      setOffset(rows.length);
    } else {
      // Append older cycles at the bottom, dropping any the sliding offset
      // window re-fetched (a new cycle started mid-browse).
      setLogs((prev) => mergeUniqueById(rows, prev, "back"));
      setOffset((prev) => prev + rows.length);
    }
    setHasMore(rows.length === PAGE_SIZE);
    setLoading(false);
  };

  useEffect(() => {
    load(true);
    // `load` closes over offset/state setters; re-running only on filter
    // changes is intentional — adding `load` would fire on every pagination.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preset, committedFrom, committedTo]);

  return (
    <div>
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: ".5rem",
          marginBottom: "1rem",
          alignItems: "center",
        }}
      >
        <TimeWindowControls
          preset={preset}
          customFrom={customFrom}
          customTo={customTo}
          onPreset={(p) => {
            setPreset(p);
          }}
          onCustomFrom={setCustomFrom}
          onCustomTo={setCustomTo}
          onApply={() => {
            setCommittedFrom(customFrom);
            setCommittedTo(customTo);
          }}
        />
        <button className="btn btn-secondary btn-sm" onClick={() => load(true)}>
          ↻ Refresh
        </button>
      </div>

      {loading && logs.length === 0 ? (
        <div className="loading">
          <div className="spinner" /> Loading logs…
        </div>
      ) : logs.length === 0 ? (
        <div className="card">
          <div className="empty-state">
            <p>No cycle logs in this time window.</p>
          </div>
        </div>
      ) : (
        <div className="card">
          <div className="table-wrap">
            <table className="table-cards">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Thermostat</th>
                  <th>Mode</th>
                  <th>Outcome</th>
                  <th>Started</th>
                  <th>Ended</th>
                  <th>Duration</th>
                  <th>Rooms</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {/* Rendered live even under CI: the pinned demo-week window
                    (see the state init above) makes every row deterministic,
                    so the goldens exercise the real table rendering instead
                    of a frozen placeholder. */}
                {logs.map((l) => (
                  <LogRow key={l.id} log={l} />
                ))}
              </tbody>
            </table>
          </div>
          {hasMore && (
            <div style={{ padding: ".75rem 1rem", borderTop: "1px solid var(--gray-200)" }}>
              <button
                className="btn btn-secondary btn-sm"
                onClick={() => load(false)}
                disabled={loading}
              >
                {loading ? "Loading…" : "Load more"}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Live Feed tab
// ---------------------------------------------------------------------------

const LEVEL_COLORS: Record<string, string> = {
  info: "var(--blue)",
  warning: "var(--orange)",
  error: "var(--red)",
};

// Every category the backend emits (see `emit()` in routes.py and
// `EventLogger.log` call sites), plus the "all" pass-through. Kept in
// lockstep with the backend by test_event_log_categories.py — a category
// with no chip here is one a user cannot isolate (Issue #580).
const CATEGORIES = ["all", "system", "api", "auth", "engine", "presence", "ha", "dev", "reconcile"];
const ALL_LEVELS = ["info", "warning", "error"];

function EventEntry({ entry }: { entry: EventLogEntry }) {
  const [expanded, setExpanded] = useState(false);
  const ts = new Date(entry.timestamp + "Z").toLocaleTimeString();
  const color = LEVEL_COLORS[entry.level] ?? "var(--gray-600)";

  return (
    <div
      className="event-entry"
      onClick={() => entry.details && setExpanded((e) => !e)}
      style={{ cursor: entry.details ? "pointer" : "default" }}
    >
      <span className="event-ts">{ts}</span>
      <span className="event-level" style={{ color }}>
        {entry.level.toUpperCase()}
      </span>
      <span className="event-category">[{entry.category}]</span>
      <span className="event-msg">{entry.message}</span>
      {entry.details && <span className="event-expand">{expanded ? "▲" : "▼"}</span>}
      {expanded && entry.details && (
        <pre className="event-details">{JSON.stringify(entry.details, null, 2)}</pre>
      )}
    </div>
  );
}

function LiveFeed() {
  const [entries, setEntries] = useState<EventLogEntry[]>([]);
  const [category, setCategory] = useState("all");
  const [levels, setLevels] = useState<string[]>(ALL_LEVELS);
  // Under CI the feed starts paused: the initial fetch (pinned to the seeded
  // demo week below) is deterministic, but websocket pushes from the live
  // engine would append mid-run and change the golden between the update and
  // verify passes.
  const [paused, setPaused] = useState(ciPinned(false, true));
  const [loading, setLoading] = useState(true);
  const [hasMore, setHasMore] = useState(false);
  const [offset, setOffset] = useState(0);
  const [showClearModal, setShowClearModal] = useState(false);
  const [clearing, setClearing] = useState(false);

  // Pinned to the seeded demo week under CI — the Metrics-page pattern
  // (Issue #442): the feed then renders the REAL seeded events in the
  // goldens; live engine events (dated "now") fall outside the window.
  const [preset, setPreset] = useState<TimePreset>(ciPinned("1h", "custom"));
  const [customFrom, setCustomFrom] = useState(ciPinned("", CI_LOGS_RANGE.from));
  const [customTo, setCustomTo] = useState(ciPinned("", CI_LOGS_RANGE.to));
  const [committedFrom, setCommittedFrom] = useState(ciPinned("", CI_LOGS_RANGE.from));
  const [committedTo, setCommittedTo] = useState(ciPinned("", CI_LOGS_RANGE.to));

  const feedRef = useRef<HTMLDivElement>(null);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  const buildParams = (currentOffset: number) => {
    const since =
      preset !== "custom"
        ? presetToSince(preset)
        : committedFrom
          ? new Date(committedFrom).toISOString()
          : undefined;
    const until =
      preset === "custom" && committedTo ? new Date(committedTo).toISOString() : undefined;
    return {
      limit: PAGE_SIZE,
      offset: currentOffset,
      category: category !== "all" ? category : undefined,
      since,
      until,
      levels: levels.length < ALL_LEVELS.length ? levels : undefined,
    };
  };

  const loadSeqRef = useRef(0);

  const load = async (reset = false) => {
    const seq = ++loadSeqRef.current;
    setLoading(true);
    const nextOffset = reset ? 0 : offset;
    const rows = await getEventLogs(buildParams(nextOffset));
    // Ignore a response that a newer load (rapid filter switch) superseded, so
    // a slow stale fetch can't overwrite fresher data. (Issue #302)
    if (seq !== loadSeqRef.current) return;
    // API returns newest-first; reverse so feed shows oldest-at-top
    const ordered = [...rows].reverse();
    if (reset) {
      setEntries(ordered);
      setOffset(rows.length);
    } else {
      // Load more prepends older entries at the top, dropping any the sliding
      // offset window re-fetched (new events arrived at the head meanwhile).
      setEntries((prev) => mergeUniqueById(ordered, prev, "front"));
      setOffset((prev) => prev + rows.length);
    }
    setHasMore(rows.length === PAGE_SIZE);
    setLoading(false);
  };

  useEffect(() => {
    load(true);
    // Re-running only on filter changes is intentional — see note above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [category, levels, preset, committedFrom, committedTo]);

  // WebSocket: append new events in real time (unless paused or filtered out)
  useEffect(() => {
    const cleanup = connectWS((event) => {
      if (event.type === "log_event" && !pausedRef.current) {
        const entry = event.data as unknown as EventLogEntry;
        const catOk = category === "all" || entry.category === category;
        const lvlOk = levels.includes(entry.level);
        if (catOk && lvlOk) {
          // Skip if this id is already shown (a load races a WS push). (Issue #302)
          setEntries((prev) =>
            prev.some((e) => e.id === entry.id) ? prev : [...prev.slice(-499), entry]
          );
        }
      }
    });
    return cleanup;
  }, [category, levels]);

  // Auto-scroll the feed container (not the page) when entries change
  useEffect(() => {
    if (!paused && feedRef.current) {
      feedRef.current.scrollTop = feedRef.current.scrollHeight;
    }
  }, [entries, paused]);

  const toggleLevel = (lv: string) =>
    setLevels((prev) => (prev.includes(lv) ? prev.filter((l) => l !== lv) : [...prev, lv]));

  const handleClear = async () => {
    setClearing(true);
    try {
      await clearEventLogs();
      setEntries([]);
      setOffset(0);
      setHasMore(false);
    } finally {
      setClearing(false);
      setShowClearModal(false);
    }
  };

  if (loading && entries.length === 0) {
    return (
      <div className="loading">
        <div className="spinner" /> Loading events…
      </div>
    );
  }

  return (
    <div>
      {/* Filter bar */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: ".5rem",
          marginBottom: ".75rem",
          alignItems: "center",
        }}
      >
        <TimeWindowControls
          preset={preset}
          customFrom={customFrom}
          customTo={customTo}
          onPreset={(p) => {
            setPreset(p);
          }}
          onCustomFrom={setCustomFrom}
          onCustomTo={setCustomTo}
          onApply={() => {
            setCommittedFrom(customFrom);
            setCommittedTo(customTo);
          }}
        />
      </div>
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: ".5rem",
          marginBottom: ".75rem",
          alignItems: "center",
        }}
      >
        {/* Category */}
        <select
          className="form-control form-control-sm"
          value={category}
          onChange={(e) => setCategory(e.target.value)}
        >
          {CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {c === "all" ? "All categories" : c}
            </option>
          ))}
        </select>

        {/* Level toggles */}
        <span className="text-sm text-muted">Levels:</span>
        {ALL_LEVELS.map((lv) => (
          <button
            key={lv}
            className={`btn btn-sm ${levels.includes(lv) ? "btn-primary" : "btn-secondary"}`}
            style={{ color: levels.includes(lv) ? undefined : "var(--gray-500)" }}
            onClick={() => toggleLevel(lv)}
          >
            {lv}
          </button>
        ))}

        <div style={{ marginLeft: "auto", display: "flex", gap: ".5rem", alignItems: "center" }}>
          <span className="text-sm text-muted">{entries.length} events</span>
          <button
            className={`btn btn-sm ${paused ? "btn-primary" : "btn-secondary"}`}
            onClick={() => setPaused((p) => !p)}
          >
            {paused ? "▶ Resume" : "⏸ Pause"}
          </button>
          <button
            className="btn btn-sm btn-danger"
            onClick={() => setShowClearModal(true)}
            disabled={clearing}
          >
            Clear logs
          </button>
        </div>
      </div>

      {/* Load more (older entries) */}
      {hasMore && (
        <div style={{ marginBottom: ".5rem" }}>
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => load(false)}
            disabled={loading}
          >
            {loading ? "Loading…" : "Load older entries"}
          </button>
        </div>
      )}

      {/* Rendered live even under CI: the pinned demo-week window plus the
          paused-under-CI websocket (see the state init above) make the feed
          deterministic, so the goldens exercise the real entry rendering. */}
      <div className="card event-feed" ref={feedRef}>
        {entries.length === 0 ? (
          <div className="empty-state">
            <p>No events in this time window.</p>
          </div>
        ) : (
          entries.map((e, i) => <EventEntry key={e.id ?? i} entry={e} />)
        )}
      </div>

      {showClearModal && (
        <ConfirmDialog
          title="Clear all event logs?"
          message="This will permanently delete all event log entries from the database. This action cannot be undone."
          confirmLabel="Clear all logs"
          onConfirm={handleClear}
          onCancel={() => setShowClearModal(false)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Retention settings tab
// ---------------------------------------------------------------------------

function RetentionSettings() {
  const [form, setForm] = useState<LogRetentionSettings>({
    event_log_retention_days: 7,
    cycle_log_retention_days: 30,
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  // Cancel the "Saved!" reset timer on unmount so it can't fire setSaved()
  // after the component is gone (otherwise the callback hits a torn-down
  // jsdom — "window is not defined" — in CI).
  const savedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    getLogRetention()
      .then((data) => {
        setForm(data);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  useEffect(() => {
    return () => {
      if (savedTimerRef.current !== null) clearTimeout(savedTimerRef.current);
    };
  }, []);

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      const updated = await setLogRetention(form);
      setForm(updated);
      setSaved(true);
      if (savedTimerRef.current !== null) clearTimeout(savedTimerRef.current);
      savedTimerRef.current = setTimeout(() => setSaved(false), 2000);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  if (loading)
    return (
      <div className="loading">
        <div className="spinner" /> Loading settings…
      </div>
    );

  return (
    <div className="card" style={{ maxWidth: 560 }}>
      <div className="card-title" style={{ marginBottom: ".25rem" }}>
        Log Retention
      </div>
      <p className="text-sm text-muted" style={{ marginBottom: "1.5rem" }}>
        Configure how long log data is kept. The scheduler runs a purge daily and on each startup.
      </p>

      {error && (
        <div className="badge badge-red" style={{ marginBottom: "1rem" }}>
          {error}
        </div>
      )}

      <div className="form-group">
        <label className="form-label">Event log retention (days)</label>
        <input
          className="form-control"
          type="number"
          min="1"
          value={form.event_log_retention_days}
          onChange={(e) =>
            setForm((f) => ({
              ...f,
              event_log_retention_days: Math.max(1, parseInt(e.target.value) || 1),
            }))
          }
        />
        <div className="form-hint">
          Event logs capture every engine action, vent movement, presence event, and state change.
          High volume — recommended 7 days. Older rows are deleted automatically.
        </div>
      </div>

      <div className="form-group">
        <label className="form-label">Cycle history retention (days)</label>
        <input
          className="form-control"
          type="number"
          min="1"
          value={form.cycle_log_retention_days}
          onChange={(e) =>
            setForm((f) => ({
              ...f,
              cycle_log_retention_days: Math.max(1, parseInt(e.target.value) || 1),
            }))
          }
        />
        <div className="form-hint">
          Cycle history records one entry per HVAC cycle (start/stop, rooms, duration). Much lower
          volume than event logs — safe to keep for 30+ days.
        </div>
      </div>

      <div style={{ display: "flex", gap: ".75rem", alignItems: "center" }}>
        <button className="btn btn-primary" onClick={save} disabled={saving}>
          {saving ? "Saving…" : "Save"}
        </button>
        {saved && <span className="badge badge-green">Saved!</span>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Logs page (tabbed)
// ---------------------------------------------------------------------------

export default function Logs() {
  const [tab, setTab] = useState<"feed" | "history" | "retention">("feed");

  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-title">Logs</div>
          <div className="page-subtitle">
            Cycle history, live event feed, and retention settings
          </div>
        </div>
        <div className="tab-bar">
          <button
            className={`tab-btn ${tab === "feed" ? "active" : ""}`}
            onClick={() => setTab("feed")}
          >
            Live Feed
          </button>
          <button
            className={`tab-btn ${tab === "history" ? "active" : ""}`}
            onClick={() => setTab("history")}
          >
            Cycle History
          </button>
          <button
            className={`tab-btn ${tab === "retention" ? "active" : ""}`}
            onClick={() => setTab("retention")}
          >
            Retention
          </button>
        </div>
      </div>

      {tab === "feed" && <LiveFeed />}
      {tab === "history" && <CycleHistory />}
      {tab === "retention" && <RetentionSettings />}
    </div>
  );
}
