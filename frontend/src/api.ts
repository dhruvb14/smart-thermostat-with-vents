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

export interface RoomSensor { id: string; room_id: string; entity_id: string; }
export interface RoomVent { id: string; room_id: string; entity_id: string; }
export interface RoomPresenceSensor { id: string; room_id: string; entity_id: string; }

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
// Base fetch helper
// ---------------------------------------------------------------------------

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
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
    method: "POST", body: JSON.stringify({ entity_id }),
  });
export const removeSensor = (room_id: string, entity_id: string) =>
  api<unknown>(`/api/rooms/${room_id}/sensors/${entity_id}`, { method: "DELETE" });

// Vents
export const addVent = (room_id: string, entity_id: string) =>
  api<RoomVent>(`/api/rooms/${room_id}/vents`, {
    method: "POST", body: JSON.stringify({ entity_id }),
  });
export const removeVent = (room_id: string, entity_id: string) =>
  api<unknown>(`/api/rooms/${room_id}/vents/${entity_id}`, { method: "DELETE" });

// Presence sensors
export const addPresence = (room_id: string, entity_id: string) =>
  api<RoomPresenceSensor>(`/api/rooms/${room_id}/presence`, {
    method: "POST", body: JSON.stringify({ entity_id }),
  });
export const removePresence = (room_id: string, entity_id: string) =>
  api<unknown>(`/api/rooms/${room_id}/presence/${entity_id}`, { method: "DELETE" });

// Overrides
export const setOverride = (room_id: string, target_temp: number, duration_hours = 2) =>
  api<unknown>(`/api/rooms/${room_id}/override`, {
    method: "POST", body: JSON.stringify({ target_temp, duration_hours }),
  });
export const clearOverride = (room_id: string) =>
  api<unknown>(`/api/rooms/${room_id}/override`, { method: "DELETE" });

// ---------------------------------------------------------------------------
// Schedules
// ---------------------------------------------------------------------------

export const getSchedules = (room_id: string) =>
  api<Schedule[]>(`/api/rooms/${room_id}/schedules`);
export const createSchedule = (room_id: string, data: Omit<Schedule, "id" | "room_id">) =>
  api<Schedule>(`/api/rooms/${room_id}/schedules`, {
    method: "POST", body: JSON.stringify(data),
  });
export const updateSchedule = (room_id: string, schedule_id: string, data: Partial<Schedule>) =>
  api<Schedule>(`/api/rooms/${room_id}/schedules/${schedule_id}`, {
    method: "PUT", body: JSON.stringify(data),
  });
export const deleteSchedule = (room_id: string, schedule_id: string) =>
  api<unknown>(`/api/rooms/${room_id}/schedules/${schedule_id}`, { method: "DELETE" });

// ---------------------------------------------------------------------------
// Thermostats
// ---------------------------------------------------------------------------

export const getThermostats = () => api<ThermostatConfig[]>("/api/thermostats");
export const createThermostat = (data: { thermostat_entity_id: string } & Partial<ThermostatConfig>) =>
  api<ThermostatConfig>("/api/thermostats", { method: "POST", body: JSON.stringify(data) });
export const updateThermostat = (entity_id: string, data: Partial<ThermostatConfig>) =>
  api<ThermostatConfig>(`/api/thermostats/${entity_id}`, {
    method: "PUT", body: JSON.stringify(data),
  });
export const deleteThermostat = (entity_id: string) =>
  api<{ deleted: string }>(`/api/thermostats/${entity_id}`, { method: "DELETE" });

// ---------------------------------------------------------------------------
// System
// ---------------------------------------------------------------------------

export const getStatus = () => api<ZoneStatus[]>("/api/status");
export const getLogs = (limit = 50) => api<CycleLog[]>(`/api/logs?limit=${limit}`);
export const getEventLogs = (limit = 200, category?: string) => {
  const params = new URLSearchParams({ limit: String(limit) });
  if (category) params.set("category", category);
  return api<EventLogEntry[]>(`/api/logs/events?${params}`);
};
export const getSystemStatus = () => api<SystemStatus>("/api/system/status");
export const setSystemEnabled = (enabled: boolean) =>
  api<SystemStatus>("/api/system/enabled", { method: "POST", body: JSON.stringify({ enabled }) });
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
  const ws = new WebSocket(`${proto}//${location.host}/ws`);

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
