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

  it("connectWS sets up WebSocket connection", () => {
    let messageHandler: (e: { data: string }) => void = () => {};
    let closeHandler: () => void = () => {};

    const mockWS = {
      addEventListener: vi.fn((event: string, handler: (e: { data: string }) => void) => {
        if (event === "message") messageHandler = handler;
        if (event === "close") closeHandler = handler as unknown as () => void;
      }),
      close: vi.fn(),
    };

    vi.stubGlobal(
      "WebSocket",
      vi.fn().mockImplementation(() => mockWS)
    );

    vi.useFakeTimers();

    try {
      const callback = vi.fn();
      const cleanup = api.connectWS(callback);

      expect(mockWS.addEventListener).toHaveBeenCalledWith("message", expect.any(Function));
      expect(mockWS.addEventListener).toHaveBeenCalledWith("close", expect.any(Function));

      // Test message receipt
      messageHandler({ data: JSON.stringify({ type: "test", data: {} }) });
      expect(callback).toHaveBeenCalledWith({ type: "test", data: {} });

      // Test malformed message
      messageHandler({ data: "not json" });
      expect(callback).toHaveBeenCalledTimes(1);

      // Test reconnect on close
      closeHandler();
      vi.advanceTimersByTime(3000);
      // Should have called connectWS again (which calls addEventListener again)
      expect(mockWS.addEventListener).toHaveBeenCalledTimes(4); // 2 more for reconnect

      cleanup();
      expect(mockWS.close).toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });
});
