import { useEffect, useState } from "react";
import {
  getRooms,
  getSchedules,
  createSchedule,
  updateSchedule,
  deleteSchedule,
  copySchedule,
  type Room,
  type Schedule,
  type ScheduleCopyResult,
} from "../api";
import { useUnit } from "../contexts";
import ConfirmDialog from "../components/ConfirmDialog";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function DayPicker({
  selected,
  onChange,
}: {
  selected: number[];
  onChange: (days: number[]) => void;
}) {
  const toggle = (d: number) => {
    if (selected.includes(d)) onChange(selected.filter((x) => x !== d));
    else onChange([...selected, d].sort());
  };
  return (
    <div className="day-picker">
      {DAYS.map((label, i) => (
        <button
          key={i}
          type="button"
          className={`day-btn${selected.includes(i) ? " selected" : ""}`}
          onClick={() => toggle(i)}
        >
          {label[0]}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Overlap detection (mirrors backend logic)
// ---------------------------------------------------------------------------
function scheduleIntervals(days: number[], start: string, end: string): [number, number, number][] {
  const toMin = (t: string) => {
    const [h, m] = t.split(":").map(Number);
    return h * 60 + (m || 0);
  };
  const sm = toMin(start),
    em = toMin(end);
  const isOvernight = em <= sm;
  const result: [number, number, number][] = [];
  for (const d of days) {
    if (!isOvernight) {
      result.push([d, sm, em]);
    } else {
      result.push([d, sm, 1440]);
      result.push([(d + 1) % 7, 0, em]);
    }
  }
  return result;
}

function schedulesOverlap(
  a: { days: number[]; start: string; end: string },
  b: { days: number[]; start: string; end: string }
): boolean {
  const aI = scheduleIntervals(a.days, a.start, a.end);
  const bI = scheduleIntervals(b.days, b.start, b.end);
  for (const [ad, as_, ae] of aI) {
    for (const [bd, bs, be] of bI) {
      if (ad === bd && as_ < be && bs < ae) return true;
    }
  }
  return false;
}

// `expires_at` from the API is a naive local ISO string ("2026-07-01T22:00:00")
// or null. `<input type="datetime-local">` wants "YYYY-MM-DDTHH:MM", so slice.
const toDatetimeLocal = (iso: string | null): string => (iso ? iso.slice(0, 16) : "");
// Compact display for the table, e.g. "Jul 1, 22:00".
const fmtExpiry = (iso: string | null): string => {
  if (!iso) return "Never";
  return iso.slice(0, 16).replace("T", " ");
};

function ScheduleModal({
  schedule,
  roomId,
  roomDeadbandOverride,
  existingSchedules,
  onClose,
  onSave,
}: {
  schedule: Schedule | null;
  roomId: string;
  /** The room's own deadband override in °F, or null when it inherits the
   *  thermostat's. Display-only: it names the value this block REPLACES so the
   *  control cannot be read as additive. */
  roomDeadbandOverride: number | null;
  existingSchedules: Schedule[];
  onClose: () => void;
  onSave: () => void;
}) {
  const { toDisplay, toDisplayDelta, displayBound, unitLabel } = useUnit();
  // Bounds the backend will actually accept, in display units. See
  // displayBound: converting a °F limit for display rounds, and rounding can
  // move the bound outward, so the raw conversion advertises values that 400.
  const minTemp = displayBound(40, "min");
  const maxTemp = displayBound(90, "max");
  const maxDeadband = displayBound(10, "max", "delta");
  // The band this block would replace, in display units — null when the room
  // inherits the thermostat's deadband, which this modal does not load.
  const inheritedDeadband =
    roomDeadbandOverride != null ? toDisplayDelta(roomDeadbandOverride) : null;
  const [days, setDays] = useState<number[]>(schedule?.days_of_week ?? [0, 1, 2, 3, 4]);
  const [start, setStart] = useState(schedule?.start_time ?? "22:00");
  const [end, setEnd] = useState(schedule?.end_time ?? "07:00");
  const [temp, setTemp] = useState(
    schedule?.target_temp != null ? String(toDisplay(schedule.target_temp)) : String(toDisplay(72))
  );
  // Per-block deadband override (Issue #517). "inherit" (default) sends null;
  // "custom" sends the raw DISPLAY delta — the backend's _delta_to_f converts.
  const [deadbandMode, setDeadbandMode] = useState<"inherit" | "custom">(
    schedule?.deadband_override != null ? "custom" : "inherit"
  );
  // Clamp what a STORED band displays as, not just what the user may type.
  // 10 °F is the documented maximum and the backend accepts it inclusively, but
  // it has no 2dp °C form that survives the round trip: toDisplayDelta(10) is
  // 5.56, which converts back to 10.01 and is refused. Without this clamp a
  // °C household that opens such a block gets 5.56 in a field capped at 5.55,
  // and EVERY save is rejected — including edits to the days or the target,
  // fields the user did touch — naming a band they never touched. Clamping
  // shows 5.55 (9.99 °F on save); 0.01 °F is far below anything a thermostat
  // resolves, and it is the only value in 0–10 affected.
  const [deadband, setDeadband] = useState(
    schedule?.deadband_override != null
      ? String(Math.min(toDisplayDelta(schedule.deadband_override), maxDeadband))
      : ""
  );
  // Expiry: "never" (default) or "at" a specific local datetime.
  const [expiryMode, setExpiryMode] = useState<"never" | "at">(
    schedule?.expires_at ? "at" : "never"
  );
  const [expiresAt, setExpiresAt] = useState(toDatetimeLocal(schedule?.expires_at ?? null));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const save = async () => {
    if (days.length === 0) {
      setError("Select at least one day");
      return;
    }
    if (!temp) {
      setError("Temperature required");
      return;
    }

    const t = parseFloat(temp);
    if (isNaN(t) || t < minTemp || t > maxTemp) {
      setError(
        `Target temperature must be between ${minTemp}${unitLabel} and ${maxTemp}${unitLabel}`
      );
      return;
    }

    // Deadband override is a DELTA in display units; bound 0–10 °F, matching
    // the backend's _validate_deadband_override.
    if (deadbandMode === "custom") {
      const db = parseFloat(deadband);
      if (deadband.trim() === "" || isNaN(db) || db < 0 || db > maxDeadband) {
        setError(`Deadband must be between 0${unitLabel} and ${maxDeadband}${unitLabel}`);
        return;
      }
    }

    if (expiryMode === "at" && !expiresAt) {
      setError("Pick an auto-disable date and time, or choose Never expire");
      return;
    }

    // Whether this block is (will remain) enabled. New blocks start enabled;
    // editing preserves the current state (the row toggle changes it).
    const enabled = schedule?.enabled ?? true;

    // Client-side overlap check — only enabled blocks reserve their slot, and
    // only matters when this block is itself enabled (mirrors the backend).
    if (enabled) {
      const candidate = { days, start, end };
      for (const e of existingSchedules) {
        if (schedule && e.id === schedule.id) continue; // skip self when editing
        if (!e.enabled) continue; // parked blocks don't reserve their slot
        const existing = { days: e.days_of_week, start: e.start_time, end: e.end_time };
        if (schedulesOverlap(candidate, existing)) {
          const daysStr = e.days_of_week.map((d) => DAYS[d]).join(", ");
          setError(`Overlaps with existing block on ${daysStr} ${e.start_time}–${e.end_time}`);
          return;
        }
      }
    }

    setSaving(true);
    try {
      // target_temp is sent in DISPLAY units; the backend converts to °F on the
      // write boundary via _to_f. deadband_override is a DELTA, also sent raw —
      // the backend's _delta_to_f converts it (never toStorageDelta here, #231).
      // expires_at is a datetime — sent as-is, no unit conversion (Issue #359).
      const payload = {
        days_of_week: days,
        start_time: start,
        end_time: end,
        target_temp: parseFloat(temp),
        enabled,
        deadband_override: deadbandMode === "custom" ? parseFloat(deadband) : null,
        expires_at: expiryMode === "at" ? expiresAt : null,
      };
      if (schedule) await updateSchedule(roomId, schedule.id, payload);
      else await createSchedule(roomId, payload);
      onSave();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="modal-title">{schedule ? "Edit Schedule" : "New Schedule"}</div>
        {error && (
          <div className="badge badge-red" style={{ marginBottom: "1rem" }}>
            {error}
          </div>
        )}

        <div className="form-group">
          <label className="form-label">Days of week</label>
          <DayPicker selected={days} onChange={setDays} />
          <div className="text-sm text-muted" style={{ marginTop: ".3rem" }}>
            {days.map((d) => DAYS[d]).join(", ") || "None selected"}
          </div>
        </div>

        {/* flex-basis 10rem (not 0) lets the pair wrap to stacked rows on
            narrow phones instead of squeezing the time inputs (#458). */}
        <div className="flex gap-md">
          <div className="form-group" style={{ flex: "1 1 10rem" }}>
            <label className="form-label" htmlFor="schedule-start">
              Start time
            </label>
            <input
              id="schedule-start"
              className="form-control"
              type="time"
              value={start}
              onChange={(e) => setStart(e.target.value)}
            />
          </div>
          <div className="form-group" style={{ flex: "1 1 10rem" }}>
            <label className="form-label" htmlFor="schedule-end">
              End time
            </label>
            <input
              id="schedule-end"
              className="form-control"
              type="time"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
            />
          </div>
        </div>

        <div className="form-group">
          <label className="form-label" htmlFor="schedule-temp">
            Target temperature ({unitLabel})
          </label>
          <input
            id="schedule-temp"
            className="form-control"
            type="number"
            step="0.5"
            min={minTemp}
            max={maxTemp}
            value={temp}
            onChange={(e) => setTemp(e.target.value)}
            placeholder={`e.g. ${Math.round(toDisplay(68))}`}
          />
        </div>

        <div className="form-group">
          <label className="form-label">Temperature drift</label>
          <div className="flex gap-md" style={{ marginBottom: ".4rem" }}>
            <label className="flex gap-sm" style={{ alignItems: "center" }}>
              <input
                type="radio"
                name="schedule-deadband-mode"
                checked={deadbandMode === "inherit"}
                onChange={() => setDeadbandMode("inherit")}
              />
              Use the room&rsquo;s normal deadband
            </label>
            <label className="flex gap-sm" style={{ alignItems: "center" }}>
              <input
                type="radio"
                name="schedule-deadband-mode"
                checked={deadbandMode === "custom"}
                onChange={() => setDeadbandMode("custom")}
              />
              Override deadband
            </label>
          </div>
          {deadbandMode === "custom" && (
            <input
              id="schedule-deadband"
              className="form-control"
              type="number"
              step="0.5"
              min={0}
              max={maxDeadband}
              aria-label={`Deadband (${unitLabel})`}
              value={deadband}
              onChange={(e) => setDeadband(e.target.value)}
            />
          )}
          <div className="text-sm text-muted" style={{ marginTop: ".3rem" }}>
            {deadbandMode === "custom" ? (
              <>
                This <strong>replaces</strong> the room&rsquo;s deadband while the block is running
                — it is not added to it
                {inheritedDeadband != null && (
                  <>
                    , so the room drifts &plusmn;{deadband || "?"}
                    {unitLabel} during the block instead of its usual &plusmn;{inheritedDeadband}
                    {unitLabel}
                  </>
                )}
                . The room may drift this far from target before it calls for heating or cooling, so
                a wider band saves runtime in a room nobody is using — and a narrower one holds the
                room tighter.
              </>
            ) : (
              <>
                The room keeps its usual deadband
                {inheritedDeadband != null && <> of &plusmn;{inheritedDeadband + unitLabel}</>} —
                inherited from the room&rsquo;s own override, or the thermostat&rsquo;s deadband if
                it has none.
              </>
            )}
          </div>
        </div>

        <div className="form-group">
          <label className="form-label">Expiry</label>
          <div className="flex gap-md" style={{ marginBottom: ".4rem" }}>
            <label className="flex gap-sm" style={{ alignItems: "center" }}>
              <input
                type="radio"
                name="schedule-expiry-mode"
                checked={expiryMode === "never"}
                onChange={() => setExpiryMode("never")}
              />
              Never expire
            </label>
            <label className="flex gap-sm" style={{ alignItems: "center" }}>
              <input
                type="radio"
                name="schedule-expiry-mode"
                checked={expiryMode === "at"}
                onChange={() => setExpiryMode("at")}
              />
              Auto-disable at
            </label>
          </div>
          {expiryMode === "at" && (
            <input
              id="schedule-expires-at"
              className="form-control"
              type="datetime-local"
              aria-label="Auto-disable date and time"
              value={expiresAt}
              onChange={(e) => setExpiresAt(e.target.value)}
            />
          )}
          <div className="text-sm text-muted" style={{ marginTop: ".3rem" }}>
            A temporary schedule disables itself at this time (it is not deleted). The current
            block, if running, finishes first.
          </div>
        </div>

        <div className="modal-footer">
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}

function CopyModal({
  schedule,
  sourceRoomId,
  rooms,
  onClose,
  onDone,
}: {
  schedule: Schedule;
  sourceRoomId: string;
  rooms: Room[];
  onClose: () => void;
  onDone: (results: ScheduleCopyResult[]) => void;
}) {
  const targets = rooms.filter((r) => r.id !== sourceRoomId);
  const [selected, setSelected] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const toggle = (id: string) =>
    setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));

  const copy = async () => {
    if (selected.length === 0) {
      setError("Select at least one room");
      return;
    }
    setSaving(true);
    try {
      const results = await copySchedule(sourceRoomId, schedule.id, selected);
      onDone(results);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Copy failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="modal-title">Copy schedule to other rooms</div>
        <div className="text-sm text-muted" style={{ marginBottom: "1rem" }}>
          Copies the days, times and target. The copy is created enabled and never-expiring. If it
          conflicts with an existing block in a room, it is copied disabled so you can resolve it.
        </div>
        {error && (
          <div className="badge badge-red" style={{ marginBottom: "1rem" }}>
            {error}
          </div>
        )}
        {targets.length === 0 ? (
          <p className="text-muted text-sm">No other rooms to copy to.</p>
        ) : (
          <div className="form-group">
            {targets.map((r) => (
              <label
                key={r.id}
                className="flex gap-sm"
                style={{ alignItems: "center", padding: ".25rem 0" }}
              >
                <input
                  type="checkbox"
                  checked={selected.includes(r.id)}
                  onChange={() => toggle(r.id)}
                />
                {r.name}
              </label>
            ))}
          </div>
        )}
        <div className="modal-footer">
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn btn-primary"
            onClick={copy}
            disabled={saving || targets.length === 0}
          >
            {saving ? "Copying…" : "Copy"}
          </button>
        </div>
      </div>
    </div>
  );
}

function RoomSchedules({ room, allRooms }: { room: Room; allRooms: Room[] }) {
  const { fmtTemp, toDisplayDelta, unitLabel } = useUnit();
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [expanded, setExpanded] = useState(false);
  const [showModal, setShowModal] = useState(false);
  const [editSchedule, setEditSchedule] = useState<Schedule | null>(null);
  const [copySource, setCopySource] = useState<Schedule | null>(null);
  const [actionError, setActionError] = useState("");
  const [copyResults, setCopyResults] = useState<ScheduleCopyResult[] | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Schedule | null>(null);

  const load = async () => {
    const s = await getSchedules(room.id);
    setSchedules(s);
  };

  // Load on mount for the count badge, reload when expanded for fresh data
  useEffect(() => {
    load();
    // Mount-only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (expanded) load();
    // Re-run only when expanded toggles.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expanded]);

  const del = async () => {
    if (!confirmDelete) return;
    const s = confirmDelete;
    setConfirmDelete(null);
    await deleteSchedule(room.id, s.id);
    load();
  };

  const toggleEnabled = async (s: Schedule) => {
    setActionError("");
    try {
      await updateSchedule(room.id, s.id, { enabled: !s.enabled });
      await load();
    } catch (e: unknown) {
      setActionError(e instanceof Error ? e.message : "Could not change schedule status");
    }
  };

  const activeCount = schedules.filter((s) => s.enabled).length;
  const inactiveCount = schedules.length - activeCount;

  return (
    <div className="card" style={{ marginBottom: "1rem" }}>
      <div
        className="flex-between"
        style={{ cursor: "pointer" }}
        onClick={() => setExpanded((e) => !e)}
      >
        <div>
          <div className="card-title" style={{ marginBottom: 0 }}>
            {room.name}
          </div>
          <div className="card-subtitle font-mono" style={{ marginBottom: 0 }}>
            {room.thermostat_entity_id}
          </div>
        </div>
        <div className="flex gap-sm">
          <span className="badge badge-green">{activeCount} active</span>
          {inactiveCount > 0 && (
            <span className="badge badge-orange">{inactiveCount} inactive</span>
          )}
          <span>{expanded ? "▲" : "▼"}</span>
        </div>
      </div>

      {expanded && (
        <div style={{ marginTop: "1rem" }}>
          {actionError && (
            <div className="badge badge-red" style={{ marginBottom: "1rem" }}>
              {actionError}
            </div>
          )}
          {copyResults && (
            <div className="card" style={{ marginBottom: "1rem", padding: ".75rem" }}>
              <div className="text-sm" style={{ marginBottom: ".4rem" }}>
                <strong>Copy results</strong>
              </div>
              {copyResults.map((r) => {
                const roomName = allRooms.find((x) => x.id === r.room_id)?.name ?? r.room_id;
                return (
                  <div key={r.schedule_id} className="text-sm" style={{ padding: ".15rem 0" }}>
                    {r.status === "created" ? (
                      <span className="badge badge-green">Copied</span>
                    ) : (
                      <span className="badge badge-orange">Copied (disabled)</span>
                    )}{" "}
                    {roomName}
                    {r.conflict_with ? ` — conflicts with ${r.conflict_with}` : ""}
                  </div>
                );
              })}
              <button
                className="btn btn-secondary btn-sm"
                style={{ marginTop: ".5rem" }}
                onClick={() => setCopyResults(null)}
              >
                Dismiss
              </button>
            </div>
          )}
          {schedules.length === 0 ? (
            <p className="text-muted text-sm">No schedules. Add one below.</p>
          ) : (
            <div className="table-wrap">
              <table className="table-cards">
                <thead>
                  <tr>
                    <th>Days</th>
                    <th>Start</th>
                    <th>End</th>
                    <th>Target</th>
                    <th>Status</th>
                    <th>Expires</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {schedules.map((s) => (
                    <tr key={s.id} style={s.enabled ? undefined : { opacity: 0.55 }}>
                      <td data-label="Days">{s.days_of_week.map((d) => DAYS[d][0]).join("")}</td>
                      <td data-label="Start">{s.start_time}</td>
                      <td data-label="End">{s.end_time}</td>
                      <td data-label="Target">
                        <strong>{fmtTemp(s.target_temp)}</strong>
                        {/* Wide-band blocks are identifiable without opening the
                            editor — no extra column, so the 7-col layout holds. */}
                        {s.deadband_override != null && (
                          <span className="badge badge-gray" style={{ marginLeft: ".35rem" }}>
                            ±{toDisplayDelta(s.deadband_override)}
                            {unitLabel} drift
                          </span>
                        )}
                      </td>
                      <td data-label="Status">
                        {s.enabled ? (
                          <span className="badge badge-green">Active</span>
                        ) : (
                          <span className="badge badge-gray">Disabled</span>
                        )}
                      </td>
                      <td data-label="Expires" className="text-sm">
                        {fmtExpiry(s.expires_at)}
                      </td>
                      <td>
                        <div className="flex gap-sm">
                          <button
                            className="btn btn-secondary btn-sm"
                            onClick={() => toggleEnabled(s)}
                          >
                            {s.enabled ? "Disable" : "Enable"}
                          </button>
                          <button
                            className="btn btn-secondary btn-sm"
                            onClick={() => {
                              setCopyResults(null);
                              setCopySource(s);
                            }}
                          >
                            Copy
                          </button>
                          <button
                            className="btn btn-secondary btn-sm"
                            onClick={() => {
                              setEditSchedule(s);
                              setShowModal(true);
                            }}
                          >
                            Edit
                          </button>
                          <button
                            className="btn btn-danger btn-sm"
                            onClick={() => setConfirmDelete(s)}
                          >
                            Del
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div style={{ marginTop: ".75rem" }}>
            <button
              className="btn btn-primary btn-sm"
              onClick={() => {
                setEditSchedule(null);
                setShowModal(true);
              }}
            >
              + Add schedule block
            </button>
          </div>
        </div>
      )}

      {showModal && (
        <ScheduleModal
          schedule={editSchedule}
          roomId={room.id}
          roomDeadbandOverride={room.deadband_override ?? null}
          existingSchedules={schedules}
          onClose={() => setShowModal(false)}
          onSave={() => {
            setShowModal(false);
            load();
          }}
        />
      )}

      {copySource && (
        <CopyModal
          schedule={copySource}
          sourceRoomId={room.id}
          rooms={allRooms}
          onClose={() => setCopySource(null)}
          onDone={(results) => {
            setCopySource(null);
            setCopyResults(results);
            load();
          }}
        />
      )}

      {confirmDelete && (
        <ConfirmDialog
          title="Delete schedule?"
          message="Delete this schedule? This cannot be undone."
          confirmLabel="Delete"
          onConfirm={() => void del()}
          onCancel={() => setConfirmDelete(null)}
        />
      )}
    </div>
  );
}

export default function Schedules() {
  const [rooms, setRooms] = useState<Room[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getRooms().then((r) => {
      setRooms(r);
      setLoading(false);
    });
  }, []);

  if (loading)
    return (
      <div className="loading">
        <div className="spinner" /> Loading…
      </div>
    );

  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-title">Schedules</div>
          <div className="page-subtitle">Time-based temperature targets per room</div>
        </div>
      </div>

      {rooms.length === 0 ? (
        <div className="card">
          <div className="empty-state">
            <p>
              No rooms configured yet. Go to <strong>Rooms</strong> first.
            </p>
          </div>
        </div>
      ) : (
        rooms.map((r) => <RoomSchedules key={r.id} room={r} allRooms={rooms} />)
      )}
    </div>
  );
}
