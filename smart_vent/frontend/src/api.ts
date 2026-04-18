/**
 * Typed REST client + WebSocket hook for the Flair Replacement API.
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

const _ingressMatch = location.pathname.match(/^(\/api\/hassio_ingress\/[^/]+)/);
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
export const testVent = (entity_id: string, control_method: ControlMethod, direction: "open" | "close") =>
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
  a.download = "flair.db";
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

export const getHAEntities = (
  domain: string,
  opts?: { hasAttribute?: string; excludeIcon?: string }
) => {
  const params = new URLSearchParams({ domain });
  if (opts?.hasAttribute) params.set("has_attribute", opts.hasAttribute);
  if (opts?.excludeIcon) params.set("exclude_icon", opts.excludeIcon);
  return api<HAEntity[]>(`/api/ha/entities?${params}`);
};

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
