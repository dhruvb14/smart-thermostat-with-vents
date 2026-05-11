/**
 * Typed REST client + WebSocket hook for the Plenum API.
 */

export interface Room {
  id: string;
  name: string;
  thermostat_entity_id: string;
  include_thermostat_sensor: boolean;
  system_wide_temp: number | null;
  presence_holdover_hours: number;
  notes: string;
  temp_offset: number;
  sensors?: RoomSensor[];
  vents?: RoomVent[];
  presence_sensors?: RoomPresenceSensor[];
  schedules?: Schedule[];
}

export interface RoomSensor {
  id: string;
  room_id: string;
  entity_id: string;
}
export type ControlMethod = "open_close" | "set_position" | "set_tilt_position" | "toggle";

export const CONTROL_METHOD_LABELS: Record<ControlMethod, string> = {
  open_close: "Open / close (cover.open_cover · cover.close_cover)",
  set_position: "Set position 0/100 (cover.set_cover_position)",
  set_tilt_position: "Set tilt 0/100 (cover.set_cover_tilt_position)",
  toggle: "Toggle (cover.toggle)",
};

export interface RoomVent {
  id: string;
  room_id: string;
  entity_id: string;
  control_method: ControlMethod;
}
export interface RoomPresenceSensor {
  id: string;
  room_id: string;
  entity_id: string;
}

export interface Schedule {
  id: string;
  room_id: string;
  days_of_week: number[];
  start_time: string;
  end_time: string;
  target_temp: number;
}

export interface ThermostatConfig {
  thermostat_entity_id: string;
  name: string;
  default_temp: number | null;
  min_setpoint: number;
  max_setpoint: number;
  deadband: number;
  max_vent_closed_min: number;
  min_open_vents: number;
  overshoot_delta: number;
  cycle_timeout_hours: number;
  // 0 = disabled. How often (minutes) the engine re-checks vent/thermostat state
  // against actual HA state and corrects external changes.
  reconciliation_interval_min: number;
  // How to hold the thermostat during vacation mode.
  // "range"  → heat_cool/auto with low=min_setpoint, high=max_setpoint
  // "single" → turn off; correct when a bound is breached
  vacation_hvac_mode: "range" | "single";
}

export interface VacationMode {
  enabled: boolean;
  return_at: string | null; // ISO-8601 UTC string
}

export interface ZoneStatus {
  thermostat_entity_id: string;
  cycle_state: "idle" | "running" | "terminating";
  hvac_mode: string;
  hvac_action: string;
  current_temp: number | null;
  setpoint: number | null;
  cycle_id: string | null;
  cycle_started_at: string | null;
  rooms: RoomLiveStatus[];
}

export interface RoomLiveStatus {
  room_id: string;
  avg_temp: number | null;
  vent_states: Record<string, string>;
  presence_active: boolean;
}

export interface CycleLog {
  id: string;
  thermostat_entity_id: string;
  started_at: string;
  ended_at: string | null;
  mode: string;
  rooms: Record<string, { name: string; target: number; source: string }>;
  ended_reason?: string | null;
  thermostat_temp_at_start?: number | null;
  thermostat_temp_at_end?: number | null;
  setpoint_at_start?: number | null;
  setpoint_at_end?: number | null;
  vents_at_start?: Record<string, string> | null;
  vents_at_end?: Record<string, string> | null;
}

export interface CycleRoomDetail {
  room_id: string;
  name: string | null;
  source: string | null;
  target_temp: number;
  reached_at: string | null;
  vent_closed_at: string | null;
  temp_at_start: number | null;
  temp_at_end: number | null;
  trigger_detail: Record<string, unknown> | null;
  joined_at: string | null;
}

export interface CycleVentEvent {
  id: number;
  timestamp: string;
  entity_id: string;
  room_id: string | null;
  action: string;
  reason: string | null;
}

export interface CycleSetpointEvent {
  id: number;
  timestamp: string;
  setpoint: number;
  reason: string | null;
}

export interface CycleDetail {
  cycle: CycleLog;
  rooms: CycleRoomDetail[];
  vent_events: CycleVentEvent[];
  setpoint_history: CycleSetpointEvent[];
}

export interface CycleTempSample {
  id: number;
  cycle_id: string;
  room_id: string | null;
  timestamp: string;
  room_temp: number | null;
  thermostat_temp: number | null;
  setpoint: number | null;
}

export interface HAEntity {
  entity_id: string;
  state: string;
  friendly_name: string;
}

export interface SystemStatus {
  enabled: boolean;
  dev_mode?: boolean;
}

export interface EventLogEntry {
  id: number;
  timestamp: string;
  level: "info" | "warning" | "error";
  category: string;
  message: string;
  details: Record<string, unknown> | null;
}

// ---------------------------------------------------------------------------
// Base path detection (HA Ingress compatibility)
// ---------------------------------------------------------------------------
// When served through HA Ingress the browser URL looks like:
//   https://ha.example.com/api/hassio_ingress/<token>/
// Absolute paths like /api/rooms would resolve against the HA root instead
// of through the ingress proxy, so we prefix every request with the ingress
// base path. In direct / dev mode BASE is empty and nothing changes.

const _ingressMatch =
  typeof location !== "undefined"
    ? location.pathname.match(/^(\/api\/hassio_ingress\/[^/]+)/)
    : null;
const BASE = _ingressMatch ? _ingressMatch[1] : "";

// ---------------------------------------------------------------------------
// Base fetch helper
// ---------------------------------------------------------------------------

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error ?? `HTTP ${res.status}`);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Rooms
// ---------------------------------------------------------------------------

export const getRooms = () => api<Room[]>("/api/rooms");
export const getRoom = (id: string) => api<Room>(`/api/rooms/${id}`);
export const createRoom = (data: Partial<Room>) =>
  api<Room>("/api/rooms", { method: "POST", body: JSON.stringify(data) });
export const updateRoom = (id: string, data: Partial<Room>) =>
  api<Room>(`/api/rooms/${id}`, { method: "PUT", body: JSON.stringify(data) });
export const deleteRoom = (id: string) =>
  api<{ deleted: string }>(`/api/rooms/${id}`, { method: "DELETE" });

// Sensors
export const addSensor = (room_id: string, entity_id: string) =>
  api<RoomSensor>(`/api/rooms/${room_id}/sensors`, {
    method: "POST",
    body: JSON.stringify({ entity_id }),
  });
export const removeSensor = (room_id: string, entity_id: string) =>
  api<unknown>(`/api/rooms/${room_id}/sensors/${entity_id}`, { method: "DELETE" });

// Vents
export const addVent = (
  room_id: string,
  entity_id: string,
  control_method: ControlMethod = "open_close"
) =>
  api<RoomVent>(`/api/rooms/${room_id}/vents`, {
    method: "POST",
    body: JSON.stringify({ entity_id, control_method }),
  });
export const updateVentControlMethod = (
  room_id: string,
  entity_id: string,
  control_method: ControlMethod
) =>
  api<{ updated: boolean; control_method: ControlMethod }>(
    `/api/rooms/${room_id}/vents/${entity_id}`,
    { method: "PATCH", body: JSON.stringify({ control_method }) }
  );
export const removeVent = (room_id: string, entity_id: string) =>
  api<unknown>(`/api/rooms/${room_id}/vents/${entity_id}`, { method: "DELETE" });
export const testVent = (
  entity_id: string,
  control_method: ControlMethod,
  direction: "open" | "close"
) =>
  api<{ ok: true }>("/api/vents/test", {
    method: "POST",
    body: JSON.stringify({ entity_id, control_method, direction }),
  });

// Presence sensors
export const addPresence = (room_id: string, entity_id: string) =>
  api<RoomPresenceSensor>(`/api/rooms/${room_id}/presence`, {
    method: "POST",
    body: JSON.stringify({ entity_id }),
  });
export const removePresence = (room_id: string, entity_id: string) =>
  api<unknown>(`/api/rooms/${room_id}/presence/${entity_id}`, { method: "DELETE" });

// Overrides
export const setOverride = (room_id: string, target_temp: number, duration_hours = 2) =>
  api<unknown>(`/api/rooms/${room_id}/override`, {
    method: "POST",
    body: JSON.stringify({ target_temp, duration_hours }),
  });
export const clearOverride = (room_id: string) =>
  api<unknown>(`/api/rooms/${room_id}/override`, { method: "DELETE" });

// ---------------------------------------------------------------------------
// Schedules
// ---------------------------------------------------------------------------

export const getSchedules = (room_id: string) => api<Schedule[]>(`/api/rooms/${room_id}/schedules`);
export const createSchedule = (room_id: string, data: Omit<Schedule, "id" | "room_id">) =>
  api<Schedule>(`/api/rooms/${room_id}/schedules`, {
    method: "POST",
    body: JSON.stringify(data),
  });
export const updateSchedule = (room_id: string, schedule_id: string, data: Partial<Schedule>) =>
  api<Schedule>(`/api/rooms/${room_id}/schedules/${schedule_id}`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
export const deleteSchedule = (room_id: string, schedule_id: string) =>
  api<unknown>(`/api/rooms/${room_id}/schedules/${schedule_id}`, { method: "DELETE" });

// ---------------------------------------------------------------------------
// Thermostats
// ---------------------------------------------------------------------------

export const getThermostats = () => api<ThermostatConfig[]>("/api/thermostats");
export const createThermostat = (
  data: { thermostat_entity_id: string } & Partial<ThermostatConfig>
) => api<ThermostatConfig>("/api/thermostats", { method: "POST", body: JSON.stringify(data) });
export const updateThermostat = (entity_id: string, data: Partial<ThermostatConfig>) =>
  api<ThermostatConfig>(`/api/thermostats/${entity_id}`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
export const deleteThermostat = (entity_id: string) =>
  api<{ deleted: string }>(`/api/thermostats/${entity_id}`, { method: "DELETE" });

// ---------------------------------------------------------------------------
// System
// ---------------------------------------------------------------------------

export interface LogRetentionSettings {
  event_log_retention_days: number;
  cycle_log_retention_days: number;
}

export interface EventLogParams {
  limit?: number;
  offset?: number;
  category?: string;
  since?: string;
  until?: string;
  levels?: string[];
}

export interface CycleLogParams {
  limit?: number;
  offset?: number;
  since?: string;
  until?: string;
}

export const getStatus = () => api<ZoneStatus[]>("/api/status");

export const getLogs = (params: CycleLogParams = {}) => {
  const p = new URLSearchParams();
  if (params.limit != null) p.set("limit", String(params.limit));
  if (params.offset != null) p.set("offset", String(params.offset));
  if (params.since) p.set("since", params.since);
  if (params.until) p.set("until", params.until);
  return api<CycleLog[]>(`/api/logs?${p}`);
};

export const getCycleDetail = (cycleId: string) =>
  api<CycleDetail>(`/api/logs/${encodeURIComponent(cycleId)}/detail`);

export const getCycleTempSamples = (cycleId: string, roomId?: string) => {
  const p = new URLSearchParams();
  if (roomId) p.set("room_id", roomId);
  const q = p.toString();
  return api<CycleTempSample[]>(
    `/api/logs/${encodeURIComponent(cycleId)}/temp-samples${q ? `?${q}` : ""}`
  );
};

export const getEventLogs = (params: EventLogParams = {}) => {
  const p = new URLSearchParams();
  if (params.limit != null) p.set("limit", String(params.limit));
  if (params.offset != null) p.set("offset", String(params.offset));
  if (params.category) p.set("category", params.category);
  if (params.since) p.set("since", params.since);
  if (params.until) p.set("until", params.until);
  if (params.levels?.length) p.set("level", params.levels.join(","));
  return api<EventLogEntry[]>(`/api/logs/events?${p}`);
};

export const clearEventLogs = () =>
  api<{ cleared: boolean }>("/api/logs/events", { method: "DELETE" });

export const getLogRetention = () => api<LogRetentionSettings>("/api/settings/log-retention");

export const setLogRetention = (data: Partial<LogRetentionSettings>) =>
  api<LogRetentionSettings>("/api/settings/log-retention", {
    method: "POST",
    body: JSON.stringify(data),
  });

export const getSystemStatus = () => api<SystemStatus>("/api/system/status");
export const setSystemEnabled = (enabled: boolean) =>
  api<SystemStatus>("/api/system/enabled", { method: "POST", body: JSON.stringify({ enabled }) });
export const getDevMode = () => api<{ dev_mode: boolean }>("/api/system/dev-mode");
export const setDevModeApi = (dev_mode: boolean) =>
  api<{ dev_mode: boolean }>("/api/system/dev-mode", {
    method: "POST",
    body: JSON.stringify({ dev_mode }),
  });
export const getDevLogs = (limit = 200) =>
  api<EventLogEntry[]>(`/api/logs/events?limit=${limit}&category=dev`);

export function downloadBackup(): void {
  const a = document.createElement("a");
  a.href = `${BASE}/api/backup`;
  a.download = "app.db";
  a.click();
}

export async function restoreBackup(file: File): Promise<void> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${BASE}/api/restore`, { method: "POST", body: form });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error ?? `HTTP ${res.status}`);
  }
}
export interface EntityState {
  state: string;
  numeric: number | null;
  unit: string;
  attributes: Record<string, unknown>;
}

export interface RoomActiveStatus {
  room_id: string;
  source: "schedule" | "presence" | "override" | "idle";
  target_temp: number | null;
  ends_in_seconds: number | null;
  presence_holdover_active: boolean;
  next_schedule_in_seconds: number | null;
  next_schedule_target: number | null;
  next_schedule_label: string | null;
}

export async function getEntityStates(
  entityIds: string[]
): Promise<Record<string, EntityState | null>> {
  return api<Record<string, EntityState | null>>("/api/ha/states", {
    method: "POST",
    body: JSON.stringify({ entity_ids: entityIds }),
  });
}

export const getRoomActiveStatuses = (room_ids: string[]) =>
  api<Record<string, RoomActiveStatus>>("/api/rooms/active-status", {
    method: "POST",
    body: JSON.stringify({ room_ids }),
  });

export const clearPresenceHoldover = (roomId: string): Promise<void> =>
  api<void>(`/api/rooms/${roomId}/presence/holdover`, { method: "DELETE" });

export const getHAEntities = (
  domain: string | string[],
  opts?: { hasAttribute?: string; excludeIcon?: string }
) => {
  const domainParam = Array.isArray(domain) ? domain.join(",") : domain;
  const params = new URLSearchParams({ domain: domainParam });
  if (opts?.hasAttribute) params.set("has_attribute", opts.hasAttribute);
  if (opts?.excludeIcon) params.set("exclude_icon", opts.excludeIcon);
  return api<HAEntity[]>(`/api/ha/entities?${params}`);
};

// ---------------------------------------------------------------------------
// Metrics (Issue #85)
// ---------------------------------------------------------------------------

export interface MetricsRange {
  start?: string; // YYYY-MM-DD local
  end?: string;
}

export interface MetricsSummary {
  start_date: string;
  end_date: string;
  thermostat_entity_id: string | null;
  heating_seconds: number;
  cooling_seconds: number;
  cycle_count: number;
  completed_count: number;
  timeout_count: number;
  aborted_count: number;
  avg_cycle_duration_seconds: number | null;
  duty_cycle_pct: number;
  avg_outside_temp_at_start: number | null;
  avg_outside_temp_at_end: number | null;
  thermostat_count: number;
  source_breakdown: Record<string, number>;
}

export type MetricsTimeseriesMetric =
  | "hours"
  | "cycles"
  | "avg_duration"
  | "duty_cycle"
  | "outside_temp"
  | "time_to_target"
  | "degree_minutes";

export interface MetricsTimeseriesPoint {
  period: string;
  value?: number | null;
  heating_seconds?: number;
  cooling_seconds?: number;
}

export interface MetricsTimeseries {
  thermostat_entity_id: string;
  metric: MetricsTimeseriesMetric;
  granularity: "day" | "month";
  start: string;
  end: string;
  series: MetricsTimeseriesPoint[];
}

export interface RoomMetric {
  room_id: string;
  room_name: string;
  participation_count: number;
  participation_rate: number;
  heating_seconds: number;
  cooling_seconds: number;
  avg_time_to_target_seconds: number | null;
}

export interface CyclesVsOutsideTempPoint {
  cycle_id: string;
  mode: string;
  outside_temp: number;
  outside_temp_at_end: number | null;
  duration_minutes: number;
  started_at: string;
}

export interface HourHeatmap {
  start_date: string;
  end_date: string;
  thermostat_entity_id: string;
  day_labels: string[];
  grid_seconds: number[][];
}

export interface VentTimelineEvent {
  cycle_id: string;
  timestamp: string;
  entity_id: string;
  room_id: string | null;
  action: string;
  reason: string | null;
  cycle_mode: string;
  cycle_started_at: string;
  cycle_ended_at: string;
}

export interface MetricsLive {
  thermostat_entity_id: string;
  as_of: string;
  today: MetricsSummary;
  current_cycle: {
    cycle_id: string;
    mode: string;
    started_at: string;
    thermostat_temp_at_start: number | null;
    setpoint_at_start: number | null;
    outside_temp_at_start: number | null;
  } | null;
  outside_temp_entity_id: string | null;
  current_outside_temp: number | null;
}

export interface OutsideTempEntitySetting {
  entity_id: string | null;
  current_value: number | null;
}

const _rangeQuery = (r: MetricsRange = {}) => {
  const p = new URLSearchParams();
  if (r.start) p.set("start", r.start);
  if (r.end) p.set("end", r.end);
  const qs = p.toString();
  return qs ? `?${qs}` : "";
};

export const getMetricsThermostatSummary = (entityId: string, range: MetricsRange = {}) =>
  api<MetricsSummary>(
    `/api/metrics/thermostats/${encodeURIComponent(entityId)}/summary${_rangeQuery(range)}`
  );

export const getMetricsHomeSummary = (range: MetricsRange = {}) =>
  api<MetricsSummary>(`/api/metrics/thermostats/summary${_rangeQuery(range)}`);

export const getMetricsTimeseries = (
  entityId: string,
  metric: MetricsTimeseriesMetric,
  granularity: "day" | "month" = "day",
  range: MetricsRange = {}
) => {
  const p = new URLSearchParams({ metric, granularity });
  if (range.start) p.set("start", range.start);
  if (range.end) p.set("end", range.end);
  return api<MetricsTimeseries>(
    `/api/metrics/thermostats/${encodeURIComponent(entityId)}/timeseries?${p}`
  );
};

export const getMetricsRoomBreakdown = (entityId: string, range: MetricsRange = {}) =>
  api<{ thermostat_entity_id: string; start: string; end: string; rooms: RoomMetric[] }>(
    `/api/metrics/thermostats/${encodeURIComponent(entityId)}/rooms${_rangeQuery(range)}`
  );

export const getMetricsCyclesVsOutsideTemp = (entityId: string, range: MetricsRange = {}) =>
  api<{
    thermostat_entity_id: string;
    start: string;
    end: string;
    points: CyclesVsOutsideTempPoint[];
  }>(
    `/api/metrics/thermostats/${encodeURIComponent(entityId)}/cycles-vs-outside-temp${_rangeQuery(range)}`
  );

export interface OvershootHistogram {
  thermostat_entity_id: string;
  start_date: string;
  end_date: string;
  bin_size: number;
  labels: string[];
  counts: number[];
  total_room_cycles: number;
  overshot_count: number;
  overshot_pct: number;
  max_overshoot_f: number;
  avg_overshoot_f: number;
}

export const getMetricsOvershootHistogram = (entityId: string, range: MetricsRange = {}) =>
  api<OvershootHistogram>(
    `/api/metrics/thermostats/${encodeURIComponent(entityId)}/overshoot-histogram${_rangeQuery(range)}`
  );

export const getMetricsHourHeatmap = (entityId: string, range: MetricsRange = {}) =>
  api<HourHeatmap>(
    `/api/metrics/thermostats/${encodeURIComponent(entityId)}/hour-heatmap${_rangeQuery(range)}`
  );

export const getMetricsVentTimeline = (entityId: string, range: MetricsRange = {}) =>
  api<{
    thermostat_entity_id: string;
    start: string;
    end: string;
    note: string;
    events: VentTimelineEvent[];
  }>(`/api/metrics/thermostats/${encodeURIComponent(entityId)}/vent-timeline${_rangeQuery(range)}`);

export const getMetricsLive = (entityId: string) =>
  api<MetricsLive>(`/api/metrics/thermostats/${encodeURIComponent(entityId)}/live`);

export function downloadMetricsCsv(
  range: MetricsRange,
  scope: "home" | "thermostat",
  entityId?: string
): void {
  const p = new URLSearchParams({ scope });
  if (range.start) p.set("start", range.start);
  if (range.end) p.set("end", range.end);
  if (scope === "thermostat" && entityId) p.set("entity_id", entityId);
  const a = document.createElement("a");
  a.href = `${BASE}/api/metrics/export.csv?${p}`;
  a.download = `metrics_${range.start ?? ""}_${range.end ?? ""}.csv`;
  a.click();
}

export const getOutsideTempEntity = () =>
  api<OutsideTempEntitySetting>("/api/settings/outside-temp-entity");

export const setOutsideTempEntity = (entity_id: string | null) =>
  api<OutsideTempEntitySetting>("/api/settings/outside-temp-entity", {
    method: "PUT",
    body: JSON.stringify({ entity_id }),
  });

export const triggerDailyRollup = (days_back?: number) =>
  api<{ rows_written: number; days_back: number }>("/api/metrics/rollup/daily", {
    method: "POST",
    body: JSON.stringify(days_back !== undefined ? { days_back } : {}),
  });

export const triggerMonthlyRollup = (months_back?: number) =>
  api<{ rows_written: number; months_back: number }>("/api/metrics/rollup/monthly", {
    method: "POST",
    body: JSON.stringify(months_back !== undefined ? { months_back } : {}),
  });

// ---------------------------------------------------------------------------
// App settings (temperature unit, etc.)
// ---------------------------------------------------------------------------

export interface AppSettings {
  temperature_unit: "F" | "C";
  unit_change_ack_required: boolean;
  vacation_mode: VacationMode;
}

export const getSettings = () => api<AppSettings>("/api/settings");

export const ackUnitChange = () =>
  api<{ ok: true }>("/api/settings/ack-unit-change", { method: "POST" });

export const getVacationMode = () => api<VacationMode>("/api/settings/vacation-mode");
export const enableVacationMode = (return_at: string) =>
  api<VacationMode>("/api/settings/vacation-mode", {
    method: "POST",
    body: JSON.stringify({ return_at }),
  });
export const disableVacationMode = () =>
  api<VacationMode>("/api/settings/vacation-mode", { method: "DELETE" });
export const testVacationMode = (entity_id: string) =>
  api<{ ok: true; min_setpoint: number; max_setpoint: number; thermostat_state: unknown }>(
    `/api/thermostats/${encodeURIComponent(entity_id)}/test-vacation`,
    { method: "POST" }
  );

export const restartApp = () => api<{ restarting: true }>("/api/restart", { method: "POST" });

// ---------------------------------------------------------------------------
// WebSocket hook
// ---------------------------------------------------------------------------

export type WSEvent = { type: string; data: Record<string, unknown> };
export type WSHandler = (event: WSEvent) => void;

export function connectWS(onMessage: WSHandler): () => void {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}${BASE}/ws`);

  ws.addEventListener("message", (e) => {
    try {
      onMessage(JSON.parse(e.data));
    } catch {
      // ignore malformed messages
    }
  });

  ws.addEventListener("close", () => {
    // Reconnect after 3 seconds
    setTimeout(() => connectWS(onMessage), 3000);
  });

  return () => ws.close();
}
