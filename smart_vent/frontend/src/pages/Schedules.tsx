import { useEffect, useState } from "react";
import {
  getRooms,
  getSchedules,
  createSchedule,
  updateSchedule,
  deleteSchedule,
  type Room,
  type Schedule,
} from "../api";

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

function ScheduleModal({
  schedule,
  roomId,
  existingSchedules,
  onClose,
  onSave,
}: {
  schedule: Schedule | null;
  roomId: string;
  existingSchedules: Schedule[];
  onClose: () => void;
  onSave: () => void;
}) {
  const [days, setDays] = useState<number[]>(schedule?.days_of_week ?? [0, 1, 2, 3, 4]);
  const [start, setStart] = useState(schedule?.start_time ?? "22:00");
  const [end, setEnd] = useState(schedule?.end_time ?? "07:00");
  const [temp, setTemp] = useState(schedule?.target_temp?.toString() ?? "72");
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

    // Client-side overlap check
    const candidate = { days, start, end };
    for (const e of existingSchedules) {
      if (schedule && e.id === schedule.id) continue; // skip self when editing
      const existing = { days: e.days_of_week, start: e.start_time, end: e.end_time };
      if (schedulesOverlap(candidate, existing)) {
        const daysStr = e.days_of_week.map((d) => DAYS[d]).join(", ");
        setError(`Overlaps with existing block on ${daysStr} ${e.start_time}–${e.end_time}`);
        return;
      }
    }

    setSaving(true);
    try {
      const payload = {
        days_of_week: days,
        start_time: start,
        end_time: end,
        target_temp: parseFloat(temp),
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

        <div className="flex gap-md">
          <div className="form-group" style={{ flex: 1 }}>
            <label className="form-label">Start time</label>
            <input
              className="form-control"
              type="time"
              value={start}
              onChange={(e) => setStart(e.target.value)}
            />
          </div>
          <div className="form-group" style={{ flex: 1 }}>
            <label className="form-label">End time</label>
            <input
              className="form-control"
              type="time"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
            />
          </div>
        </div>

        <div className="form-group">
          <label className="form-label">Target temperature (°F)</label>
          <input
            className="form-control"
            type="number"
            step="0.5"
            value={temp}
            onChange={(e) => setTemp(e.target.value)}
            placeholder="e.g. 68"
          />
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

function RoomSchedules({ room }: { room: Room }) {
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [expanded, setExpanded] = useState(false);
  const [showModal, setShowModal] = useState(false);
  const [editSchedule, setEditSchedule] = useState<Schedule | null>(null);

  const load = async () => {
    const s = await getSchedules(room.id);
    setSchedules(s);
  };

  // Load on mount for the count badge, reload when expanded for fresh data
  useEffect(() => {
    load();
  }, []);
  useEffect(() => {
    if (expanded) load();
  }, [expanded]);

  const del = async (s: Schedule) => {
    if (confirm("Delete this schedule?")) {
      await deleteSchedule(room.id, s.id);
      load();
    }
  };

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
          <span className="badge badge-gray">{schedules.length} blocks</span>
          <span>{expanded ? "▲" : "▼"}</span>
        </div>
      </div>

      {expanded && (
        <div style={{ marginTop: "1rem" }}>
          {schedules.length === 0 ? (
            <p className="text-muted text-sm">No schedules. Add one below.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Days</th>
                    <th>Start</th>
                    <th>End</th>
                    <th>Target</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {schedules.map((s) => (
                    <tr key={s.id}>
                      <td>{s.days_of_week.map((d) => DAYS[d][0]).join("")}</td>
                      <td>{s.start_time}</td>
                      <td>{s.end_time}</td>
                      <td>
                        <strong>{s.target_temp}°F</strong>
                      </td>
                      <td>
                        <div className="flex gap-sm">
                          <button
                            className="btn btn-secondary btn-sm"
                            onClick={() => {
                              setEditSchedule(s);
                              setShowModal(true);
                            }}
                          >
                            Edit
                          </button>
                          <button className="btn btn-danger btn-sm" onClick={() => del(s)}>
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
          existingSchedules={schedules}
          onClose={() => setShowModal(false)}
          onSave={() => {
            setShowModal(false);
            load();
          }}
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
        rooms.map((r) => <RoomSchedules key={r.id} room={r} />)
      )}
    </div>
  );
}
