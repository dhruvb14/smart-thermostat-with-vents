import { describe, it, expect, vi, beforeEach } from "vitest";
import * as api from "./api";

// Mock fetch
vi.stubGlobal("fetch", vi.fn());

describe("API Client", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  const mockJsonResponse = (data: unknown) => {
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      json: async () => data,
    } as unknown as Response);
  };

  it("getRooms fetches rooms from the correct endpoint", async () => {
    mockJsonResponse([{ id: "1", name: "Test Room" }]);
    const rooms = await api.getRooms();
    expect(fetch).toHaveBeenCalledWith("/api/rooms", expect.anything());
    expect(rooms).toEqual([{ id: "1", name: "Test Room" }]);
  });

  it("getRoom fetches a single room", async () => {
    mockJsonResponse({ id: "1", name: "Test Room" });
    const room = await api.getRoom("1");
    expect(fetch).toHaveBeenCalledWith("/api/rooms/1", expect.anything());
    expect(room.name).toBe("Test Room");
  });

  it("createRoom sends a POST request", async () => {
    mockJsonResponse({ id: "2" });
    const payload = { name: "New Room" };
    await api.createRoom(payload as api.Room);
    expect(fetch).toHaveBeenCalledWith(
      "/api/rooms",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(payload),
      })
    );
  });

  it("updateRoom sends a PUT request", async () => {
    mockJsonResponse({ id: "1" });
    await api.updateRoom("1", { name: "Updated" });
    expect(fetch).toHaveBeenCalledWith(
      "/api/rooms/1",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ name: "Updated" }),
      })
    );
  });

  it("deleteRoom sends a DELETE request", async () => {
    mockJsonResponse({ deleted: "1" });
    await api.deleteRoom("1");
    expect(fetch).toHaveBeenCalledWith(
      "/api/rooms/1",
      expect.objectContaining({
        method: "DELETE",
      })
    );
  });

  it("addSensor calls the correct sub-resource", async () => {
    mockJsonResponse({});
    await api.addSensor("r1", "s1");
    expect(fetch).toHaveBeenCalledWith(
      "/api/rooms/r1/sensors",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ entity_id: "s1" }),
      })
    );
  });

  it("removeSensor calls the correct sub-resource with DELETE", async () => {
    mockJsonResponse({});
    await api.removeSensor("r1", "s1");
    expect(fetch).toHaveBeenCalledWith(
      "/api/rooms/r1/sensors/s1",
      expect.objectContaining({
        method: "DELETE",
      })
    );
  });

  it("addVent sends control_method", async () => {
    mockJsonResponse({});
    await api.addVent("r1", "v1", "set_position");
    expect(fetch).toHaveBeenCalledWith(
      "/api/rooms/r1/vents",
      expect.objectContaining({
        body: JSON.stringify({ entity_id: "v1", control_method: "set_position" }),
      })
    );
  });

  it("updateVentControlMethod uses PATCH", async () => {
    mockJsonResponse({});
    await api.updateVentControlMethod("r1", "v1", "toggle");
    expect(fetch).toHaveBeenCalledWith(
      "/api/rooms/r1/vents/v1",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ control_method: "toggle" }),
      })
    );
  });

  it("testVent sends a POST to /api/vents/test", async () => {
    mockJsonResponse({ ok: true });
    await api.testVent("v1", "open_close", "close");
    expect(fetch).toHaveBeenCalledWith(
      "/api/vents/test",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ entity_id: "v1", control_method: "open_close", direction: "close" }),
      })
    );
  });

  it("addPresence adds a presence sensor", async () => {
    mockJsonResponse({});
    await api.addPresence("r1", "p1");
    expect(fetch).toHaveBeenCalledWith(
      "/api/rooms/r1/presence",
      expect.objectContaining({
        method: "POST",
      })
    );
  });

  it("removePresence removes a presence sensor", async () => {
    mockJsonResponse({});
    await api.removePresence("r1", "p1");
    expect(fetch).toHaveBeenCalledWith(
      "/api/rooms/r1/presence/p1",
      expect.objectContaining({
        method: "DELETE",
      })
    );
  });

  it("setOverride sets a temporary override", async () => {
    mockJsonResponse({});
    await api.setOverride("r1", 75, 3);
    expect(fetch).toHaveBeenCalledWith(
      "/api/rooms/r1/override",
      expect.objectContaining({
        body: JSON.stringify({ target_temp: 75, duration_hours: 3 }),
      })
    );
  });

  it("clearOverride deletes override", async () => {
    mockJsonResponse({});
    await api.clearOverride("r1");
    expect(fetch).toHaveBeenCalledWith(
      "/api/rooms/r1/override",
      expect.objectContaining({
        method: "DELETE",
      })
    );
  });

  it("getSchedules fetches schedules for a room", async () => {
    mockJsonResponse([]);
    await api.getSchedules("r1");
    expect(fetch).toHaveBeenCalledWith("/api/rooms/r1/schedules", expect.anything());
  });

  it("createSchedule sends a POST request", async () => {
    mockJsonResponse({});
    await api.createSchedule("r1", {
      start_time: "10:00",
      end_time: "11:00",
      days_of_week: [1],
      target_temp: 70,
      enabled: true,
      expires_at: null,
    });
    expect(fetch).toHaveBeenCalledWith(
      "/api/rooms/r1/schedules",
      expect.objectContaining({
        method: "POST",
      })
    );
  });

  it("updateSchedule sends a PUT request", async () => {
    mockJsonResponse({});
    await api.updateSchedule("r1", "s1", { target_temp: 71 });
    expect(fetch).toHaveBeenCalledWith(
      "/api/rooms/r1/schedules/s1",
      expect.objectContaining({
        method: "PUT",
      })
    );
  });

  it("deleteSchedule sends a DELETE request", async () => {
    mockJsonResponse({});
    await api.deleteSchedule("r1", "s1");
    expect(fetch).toHaveBeenCalledWith(
      "/api/rooms/r1/schedules/s1",
      expect.objectContaining({
        method: "DELETE",
      })
    );
  });

  it("getThermostats fetches all thermostats", async () => {
    mockJsonResponse([]);
    await api.getThermostats();
    expect(fetch).toHaveBeenCalledWith("/api/thermostats", expect.anything());
  });

  it("createThermostat sends a POST request", async () => {
    mockJsonResponse({});
    await api.createThermostat({ thermostat_entity_id: "climate.test", name: "Test" });
    expect(fetch).toHaveBeenCalledWith(
      "/api/thermostats",
      expect.objectContaining({
        method: "POST",
      })
    );
  });

  it("updateThermostat sends a PUT request", async () => {
    mockJsonResponse({});
    await api.updateThermostat("climate.test", { name: "Updated" });
    expect(fetch).toHaveBeenCalledWith(
      "/api/thermostats/climate.test",
      expect.objectContaining({
        method: "PUT",
      })
    );
  });

  it("deleteThermostat sends a DELETE request", async () => {
    mockJsonResponse({});
    await api.deleteThermostat("climate.test");
    expect(fetch).toHaveBeenCalledWith(
      "/api/thermostats/climate.test",
      expect.objectContaining({
        method: "DELETE",
      })
    );
  });

  it("getStatus fetches zone status", async () => {
    mockJsonResponse([]);
    await api.getStatus();
    expect(fetch).toHaveBeenCalledWith("/api/status", expect.anything());
  });

  it("getLogs handles various query params", async () => {
    mockJsonResponse([]);
    await api.getLogs({ limit: 10, offset: 5, since: "2024-01-01" });
    const url = vi.mocked(fetch).mock.calls[0][0] as string;
    expect(url).toContain("limit=10");
    expect(url).toContain("offset=5");
    expect(url).toContain("since=2024-01-01");
  });

  it("getCycleDetail fetches detail for a cycle", async () => {
    mockJsonResponse({});
    await api.getCycleDetail("c1");
    expect(fetch).toHaveBeenCalledWith("/api/logs/c1/detail", expect.anything());
  });

  it("getCycleTempSamples handles room_id", async () => {
    mockJsonResponse([]);
    await api.getCycleTempSamples("c1", "r1");
    const url = vi.mocked(fetch).mock.calls[0][0] as string;
    expect(url).toContain("/api/logs/c1/temp-samples?room_id=r1");
  });

  it("getEventLogs handles various query params", async () => {
    mockJsonResponse([]);
    await api.getEventLogs({ category: "system", levels: ["info", "error"] });
    const url = vi.mocked(fetch).mock.calls[0][0] as string;
    expect(url).toContain("category=system");
    expect(url).toContain("level=info%2Cerror");
  });

  it("clearEventLogs sends a DELETE request", async () => {
    mockJsonResponse({ cleared: true });
    await api.clearEventLogs();
    expect(fetch).toHaveBeenCalledWith(
      "/api/logs/events",
      expect.objectContaining({
        method: "DELETE",
      })
    );
  });

  it("getLogRetention fetches settings", async () => {
    mockJsonResponse({});
    await api.getLogRetention();
    expect(fetch).toHaveBeenCalledWith("/api/settings/log-retention", expect.anything());
  });

  it("setLogRetention sends a POST request", async () => {
    mockJsonResponse({});
    await api.setLogRetention({ event_log_retention_days: 14 });
    expect(fetch).toHaveBeenCalledWith(
      "/api/settings/log-retention",
      expect.objectContaining({
        method: "POST",
      })
    );
  });

  it("getSystemStatus fetches overall status", async () => {
    mockJsonResponse({ enabled: true });
    await api.getSystemStatus();
    expect(fetch).toHaveBeenCalledWith("/api/system/status", expect.anything());
  });

  it("setSystemEnabled toggles system", async () => {
    mockJsonResponse({ enabled: false });
    await api.setSystemEnabled(false);
    expect(fetch).toHaveBeenCalledWith(
      "/api/system/enabled",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ enabled: false }),
      })
    );
  });

  it("getDevLogs fetches dev category logs", async () => {
    mockJsonResponse([]);
    await api.getDevLogs(50);
    const url = vi.mocked(fetch).mock.calls[0][0] as string;
    expect(url).toContain("limit=50");
    expect(url).toContain("category=dev");
  });

  it("getRoomActiveStatuses sends a POST", async () => {
    mockJsonResponse({});
    await api.getRoomActiveStatuses(["r1", "r2"]);
    expect(fetch).toHaveBeenCalledWith(
      "/api/rooms/active-status",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ room_ids: ["r1", "r2"] }),
      })
    );
  });

  it("getHAEntities handles array of domains", async () => {
    mockJsonResponse([]);
    await api.getHAEntities(["sensor", "binary_sensor"], { hasAttribute: "unit" });
    const url = vi.mocked(fetch).mock.calls[0][0] as string;
    expect(url).toContain("domain=sensor%2Cbinary_sensor");
    expect(url).toContain("has_attribute=unit");
  });

  it("getMetricsHomeSummary uses correct endpoint", async () => {
    mockJsonResponse({});
    await api.getMetricsHomeSummary({ start: "2024-01-01" });
    const url = vi.mocked(fetch).mock.calls[0][0] as string;
    expect(url).toBe("/api/metrics/thermostats/summary?start=2024-01-01");
  });

  it("getMetricsTimeseries handles all params", async () => {
    mockJsonResponse({});
    await api.getMetricsTimeseries("climate.test", "hours", "month", { start: "2024-01-01" });
    const url = vi.mocked(fetch).mock.calls[0][0] as string;
    expect(url).toContain("/api/metrics/thermostats/climate.test/timeseries");
    expect(url).toContain("metric=hours");
    expect(url).toContain("granularity=month");
    expect(url).toContain("start=2024-01-01");
  });

  it("getOutsideTempEntity fetches setting", async () => {
    mockJsonResponse({});
    await api.getOutsideTempEntity();
    expect(fetch).toHaveBeenCalledWith("/api/settings/outside-temp-entity", expect.anything());
  });

  it("setOutsideTempEntity sends a PUT", async () => {
    mockJsonResponse({});
    await api.setOutsideTempEntity("sensor.outside");
    expect(fetch).toHaveBeenCalledWith(
      "/api/settings/outside-temp-entity",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ entity_id: "sensor.outside" }),
      })
    );
  });

  it("triggerDailyRollup sends a POST", async () => {
    mockJsonResponse({});
    await api.triggerDailyRollup(2);
    expect(fetch).toHaveBeenCalledWith(
      "/api/metrics/rollup/daily",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ days_back: 2 }),
      })
    );
  });

  it("handles API errors from JSON body", async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: false,
      status: 400,
      json: async () => ({ error: "Invalid data" }),
    } as unknown as Response);
    await expect(api.getRooms()).rejects.toThrow("Invalid data");
  });

  it("handles API errors with status code fallback", async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => {
        throw new Error("not json");
      },
    } as unknown as Response);
    await expect(api.getRooms()).rejects.toThrow("HTTP 500");
  });

  it("getAuthStatus fetches the auth probe", async () => {
    mockJsonResponse({ require_auth: true, authenticated: false, method: "none" });
    const s = await api.getAuthStatus();
    expect(fetch).toHaveBeenCalledWith("/api/auth/status", expect.anything());
    expect(s).toEqual({ require_auth: true, authenticated: false, method: "none" });
  });

  it("login POSTs the credentials", async () => {
    mockJsonResponse({ ok: true });
    await api.login("alice", "pw");
    expect(fetch).toHaveBeenCalledWith(
      "/api/auth/login",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ username: "alice", password: "pw" }),
      })
    );
  });

  it("logout POSTs to the logout endpoint", async () => {
    mockJsonResponse({ ok: true });
    await api.logout();
    expect(fetch).toHaveBeenCalledWith(
      "/api/auth/logout",
      expect.objectContaining({ method: "POST" })
    );
  });

  it("a 401 dispatches a plenum-unauthorized event", async () => {
    const spy = vi.fn();
    window.addEventListener("plenum-unauthorized", spy);
    vi.mocked(fetch).mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ error: "Authentication required" }),
    } as unknown as Response);
    await expect(api.getRooms()).rejects.toThrow("Authentication required");
    expect(spy).toHaveBeenCalledTimes(1);
    window.removeEventListener("plenum-unauthorized", spy);
  });

  it("sends the session cookie (credentials: same-origin)", async () => {
    mockJsonResponse([]);
    await api.getRooms();
    expect(fetch).toHaveBeenCalledWith(
      "/api/rooms",
      expect.objectContaining({ credentials: "same-origin" })
    );
  });

  it("sends X-Requested-With so the CSRF check passes behind a proxy", async () => {
    mockJsonResponse([]);
    await api.getRooms();
    expect(fetch).toHaveBeenCalledWith(
      "/api/rooms",
      expect.objectContaining({
        headers: expect.objectContaining({ "X-Requested-With": "XMLHttpRequest" }),
      })
    );
  });

  it("listMcpTokens fetches tokens", async () => {
    mockJsonResponse([{ id: "1", label: "t", scope: "read", created_at: "x", last_used_at: null }]);
    const tokens = await api.listMcpTokens();
    expect(fetch).toHaveBeenCalledWith("/api/mcp/tokens", expect.anything());
    expect(tokens[0].scope).toBe("read");
  });

  it("mintMcpToken POSTs label + scope", async () => {
    mockJsonResponse({ id: "1", token: "secret", scope: "write" });
    const created = await api.mintMcpToken("my token", "write");
    expect(fetch).toHaveBeenCalledWith(
      "/api/mcp/tokens",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ label: "my token", scope: "write" }),
      })
    );
    expect(created.token).toBe("secret");
  });

  it("revokeMcpToken sends a DELETE", async () => {
    mockJsonResponse({ deleted: true });
    await api.revokeMcpToken("abc");
    expect(fetch).toHaveBeenCalledWith(
      "/api/mcp/tokens/abc",
      expect.objectContaining({ method: "DELETE" })
    );
  });

  it("removeVent sends a DELETE request", async () => {
    mockJsonResponse({});
    await api.removeVent("r1", "v1");
    expect(fetch).toHaveBeenCalledWith(
      "/api/rooms/r1/vents/v1",
      expect.objectContaining({ method: "DELETE" })
    );
  });

  it("clearPresenceHoldover deletes the holdover", async () => {
    mockJsonResponse({});
    await api.clearPresenceHoldover("r1");
    expect(fetch).toHaveBeenCalledWith(
      "/api/rooms/r1/presence/holdover",
      expect.objectContaining({ method: "DELETE" })
    );
  });

  it("getDevMode fetches the dev-mode flag", async () => {
    mockJsonResponse({ dev_mode: true });
    const res = await api.getDevMode();
    expect(fetch).toHaveBeenCalledWith("/api/system/dev-mode", expect.anything());
    expect(res.dev_mode).toBe(true);
  });

  it("setDevModeApi posts the dev-mode flag", async () => {
    mockJsonResponse({ dev_mode: false });
    await api.setDevModeApi(false);
    expect(fetch).toHaveBeenCalledWith(
      "/api/system/dev-mode",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ dev_mode: false }),
      })
    );
  });

  it("getEntityStates posts entity ids", async () => {
    mockJsonResponse({ "sensor.a": null });
    await api.getEntityStates(["sensor.a"]);
    expect(fetch).toHaveBeenCalledWith(
      "/api/ha/states",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ entity_ids: ["sensor.a"] }),
      })
    );
  });

  it("getMetricsThermostatSummary encodes the entity id and range", async () => {
    mockJsonResponse({});
    await api.getMetricsThermostatSummary("climate.test", {
      start: "2024-01-01",
      end: "2024-02-01",
    });
    const url = vi.mocked(fetch).mock.calls[0][0] as string;
    expect(url).toContain("/api/metrics/thermostats/climate.test/summary");
    expect(url).toContain("start=2024-01-01");
    expect(url).toContain("end=2024-02-01");
  });

  it("getMetricsThermostatSummary omits the query string with no range", async () => {
    mockJsonResponse({});
    await api.getMetricsThermostatSummary("climate.test");
    const url = vi.mocked(fetch).mock.calls[0][0] as string;
    expect(url).toBe("/api/metrics/thermostats/climate.test/summary");
  });

  it("getMetricsRoomBreakdown hits the rooms endpoint", async () => {
    mockJsonResponse({});
    await api.getMetricsRoomBreakdown("climate.test");
    expect(fetch).toHaveBeenCalledWith(
      "/api/metrics/thermostats/climate.test/rooms",
      expect.anything()
    );
  });

  it("getMetricsCyclesVsOutsideTemp hits the cycles-vs-outside-temp endpoint", async () => {
    mockJsonResponse({});
    await api.getMetricsCyclesVsOutsideTemp("climate.test", { start: "2024-01-01" });
    const url = vi.mocked(fetch).mock.calls[0][0] as string;
    expect(url).toContain("/api/metrics/thermostats/climate.test/cycles-vs-outside-temp");
    expect(url).toContain("start=2024-01-01");
  });

  it("getMetricsOvershootHistogram hits the overshoot-histogram endpoint", async () => {
    mockJsonResponse({});
    await api.getMetricsOvershootHistogram("climate.test");
    expect(fetch).toHaveBeenCalledWith(
      "/api/metrics/thermostats/climate.test/overshoot-histogram",
      expect.anything()
    );
  });

  it("getMetricsHourHeatmap hits the hour-heatmap endpoint", async () => {
    mockJsonResponse({});
    await api.getMetricsHourHeatmap("climate.test");
    expect(fetch).toHaveBeenCalledWith(
      "/api/metrics/thermostats/climate.test/hour-heatmap",
      expect.anything()
    );
  });

  it("getMetricsVentTimeline hits the vent-timeline endpoint", async () => {
    mockJsonResponse({});
    await api.getMetricsVentTimeline("climate.test");
    expect(fetch).toHaveBeenCalledWith(
      "/api/metrics/thermostats/climate.test/vent-timeline",
      expect.anything()
    );
  });

  it("getMetricsLive hits the live endpoint", async () => {
    mockJsonResponse({});
    await api.getMetricsLive("climate.test");
    expect(fetch).toHaveBeenCalledWith(
      "/api/metrics/thermostats/climate.test/live",
      expect.anything()
    );
  });

  it("getSensorStaleness fetches the setting", async () => {
    mockJsonResponse({ stale_after_min: 30 });
    await api.getSensorStaleness();
    expect(fetch).toHaveBeenCalledWith("/api/settings/sensor-staleness", expect.anything());
  });

  it("setSensorStaleness sends a PUT", async () => {
    mockJsonResponse({ stale_after_min: 45 });
    await api.setSensorStaleness(45);
    expect(fetch).toHaveBeenCalledWith(
      "/api/settings/sensor-staleness",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ stale_after_min: 45 }),
      })
    );
  });

  it("getSensorHealth fetches the summary", async () => {
    mockJsonResponse({ stale_after_min: 30, rooms: [] });
    await api.getSensorHealth();
    expect(fetch).toHaveBeenCalledWith("/api/sensor-health", expect.anything());
  });

  it("getThermostatHealth fetches the availability summary", async () => {
    mockJsonResponse({ thermostats: [] });
    await api.getThermostatHealth();
    expect(fetch).toHaveBeenCalledWith("/api/thermostat-health", expect.anything());
  });

  it("triggerMonthlyRollup sends months_back when provided", async () => {
    mockJsonResponse({});
    await api.triggerMonthlyRollup(3);
    expect(fetch).toHaveBeenCalledWith(
      "/api/metrics/rollup/monthly",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ months_back: 3 }),
      })
    );
  });

  it("triggerMonthlyRollup omits months_back when undefined", async () => {
    mockJsonResponse({});
    await api.triggerMonthlyRollup();
    expect(fetch).toHaveBeenCalledWith(
      "/api/metrics/rollup/monthly",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({}),
      })
    );
  });

  it("triggerDailyRollup omits days_back when undefined", async () => {
    mockJsonResponse({});
    await api.triggerDailyRollup();
    expect(fetch).toHaveBeenCalledWith(
      "/api/metrics/rollup/daily",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({}),
      })
    );
  });

  it("getSettings fetches app settings", async () => {
    mockJsonResponse({ temperature_unit: "F" });
    await api.getSettings();
    expect(fetch).toHaveBeenCalledWith("/api/settings", expect.anything());
  });

  it("setThemeApi posts the theme choice", async () => {
    mockJsonResponse({ theme: "dark" });
    await api.setThemeApi("dark");
    expect(fetch).toHaveBeenCalledWith(
      "/api/settings/theme",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ theme: "dark" }),
      })
    );
  });

  it("ackUnitChange posts the acknowledgement", async () => {
    mockJsonResponse({ ok: true });
    await api.ackUnitChange();
    expect(fetch).toHaveBeenCalledWith(
      "/api/settings/ack-unit-change",
      expect.objectContaining({ method: "POST" })
    );
  });

  it("getVacationMode fetches the mode", async () => {
    mockJsonResponse({ enabled: false, return_at: null });
    await api.getVacationMode();
    expect(fetch).toHaveBeenCalledWith("/api/settings/vacation-mode", expect.anything());
  });

  it("enableVacationMode posts the return time", async () => {
    mockJsonResponse({ enabled: true, return_at: "2024-06-01T00:00:00Z" });
    await api.enableVacationMode("2024-06-01T00:00:00Z");
    expect(fetch).toHaveBeenCalledWith(
      "/api/settings/vacation-mode",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ return_at: "2024-06-01T00:00:00Z" }),
      })
    );
  });

  it("disableVacationMode sends a DELETE", async () => {
    mockJsonResponse({ enabled: false, return_at: null });
    await api.disableVacationMode();
    expect(fetch).toHaveBeenCalledWith(
      "/api/settings/vacation-mode",
      expect.objectContaining({ method: "DELETE" })
    );
  });

  it("testVacationMode posts to the test endpoint", async () => {
    mockJsonResponse({ ok: true });
    await api.testVacationMode("climate.test");
    expect(fetch).toHaveBeenCalledWith(
      "/api/thermostats/climate.test/test-vacation",
      expect.objectContaining({ method: "POST" })
    );
  });

  it("revertVacationTest deletes the test state", async () => {
    mockJsonResponse({ ok: true });
    await api.revertVacationTest("climate.test");
    expect(fetch).toHaveBeenCalledWith(
      "/api/thermostats/climate.test/test-vacation",
      expect.objectContaining({ method: "DELETE" })
    );
  });

  it("restartApp posts to the restart endpoint", async () => {
    mockJsonResponse({ restarting: true });
    await api.restartApp();
    expect(fetch).toHaveBeenCalledWith("/api/restart", expect.objectContaining({ method: "POST" }));
  });

  it("getMetricsHomeSummary omits the query string with no range", async () => {
    mockJsonResponse({});
    await api.getMetricsHomeSummary();
    const url = vi.mocked(fetch).mock.calls[0][0] as string;
    expect(url).toBe("/api/metrics/thermostats/summary");
  });

  describe("anchor-driven downloads", () => {
    let clickSpy: ReturnType<typeof vi.fn>;
    let anchor: { href: string; download: string; click: ReturnType<typeof vi.fn> };

    beforeEach(() => {
      clickSpy = vi.fn();
      anchor = { href: "", download: "", click: clickSpy };
      vi.spyOn(document, "createElement").mockReturnValue(anchor as unknown as HTMLAnchorElement);
    });

    it("downloadBackup builds an anchor to /api/backup", () => {
      api.downloadBackup();
      expect(anchor.href).toBe("/api/backup");
      expect(anchor.download).toBe("app.db");
      expect(clickSpy).toHaveBeenCalled();
    });

    it("downloadMetricsCsv encodes home scope", () => {
      api.downloadMetricsCsv({ start: "2024-01-01", end: "2024-02-01" }, "home");
      expect(anchor.href).toContain("/api/metrics/export.csv?");
      expect(anchor.href).toContain("scope=home");
      expect(anchor.href).toContain("start=2024-01-01");
      expect(anchor.href).toContain("end=2024-02-01");
      expect(anchor.href).not.toContain("entity_id");
      expect(clickSpy).toHaveBeenCalled();
    });

    it("downloadMetricsCsv adds entity_id for thermostat scope", () => {
      api.downloadMetricsCsv({}, "thermostat", "climate.test");
      expect(anchor.href).toContain("scope=thermostat");
      expect(anchor.href).toContain("entity_id=climate.test");
    });
  });

  describe("restoreBackup", () => {
    it("posts the file as multipart form data", async () => {
      vi.mocked(fetch).mockResolvedValue({ ok: true } as unknown as Response);
      const file = new File(["data"], "app.db");
      await api.restoreBackup(file);
      const [url, init] = vi.mocked(fetch).mock.calls[0];
      expect(url).toBe("/api/restore");
      expect((init as RequestInit).method).toBe("POST");
      expect((init as RequestInit).body).toBeInstanceOf(FormData);
    });

    it("throws the server error message on failure", async () => {
      vi.mocked(fetch).mockResolvedValue({
        ok: false,
        status: 422,
        json: async () => ({ error: "Bad backup" }),
      } as unknown as Response);
      await expect(api.restoreBackup(new File(["x"], "app.db"))).rejects.toThrow("Bad backup");
    });

    it("falls back to the status code when the body is not JSON", async () => {
      vi.mocked(fetch).mockResolvedValue({
        ok: false,
        status: 500,
        json: async () => {
          throw new Error("not json");
        },
      } as unknown as Response);
      await expect(api.restoreBackup(new File(["x"], "app.db"))).rejects.toThrow("HTTP 500");
    });
  });

  it("connectWS reconnects while live but not after an intentional dispose", () => {
    let messageHandler: (e: { data: string }) => void = () => {};
    let closeHandler: () => void = () => {};
    let constructCount = 0;

    const mockWS = {
      addEventListener: vi.fn((event: string, handler: (e: { data: string }) => void) => {
        if (event === "message") messageHandler = handler;
        if (event === "close") closeHandler = handler as unknown as () => void;
      }),
      close: vi.fn(),
    };

    vi.stubGlobal(
      "WebSocket",
      vi.fn(function () {
        constructCount += 1;
        return mockWS;
      })
    );

    vi.useFakeTimers();

    try {
      const callback = vi.fn();
      const cleanup = api.connectWS(callback);

      expect(constructCount).toBe(1);
      expect(mockWS.addEventListener).toHaveBeenCalledWith("message", expect.any(Function));
      expect(mockWS.addEventListener).toHaveBeenCalledWith("close", expect.any(Function));

      // Message receipt
      messageHandler({ data: JSON.stringify({ type: "test", data: {} }) });
      expect(callback).toHaveBeenCalledWith({ type: "test", data: {} });

      // Malformed messages are ignored
      messageHandler({ data: "not json" });
      expect(callback).toHaveBeenCalledTimes(1);

      // An unexpected close while still live reconnects after 3s
      closeHandler();
      vi.advanceTimersByTime(3000);
      expect(constructCount).toBe(2);

      // After dispose, the socket is closed AND a subsequent close event must
      // NOT spawn a replacement — otherwise zombie sockets accumulate. (#283)
      cleanup();
      expect(mockWS.close).toHaveBeenCalled();
      closeHandler();
      vi.advanceTimersByTime(10000);
      expect(constructCount).toBe(2);
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });
});
