import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as api from "./api";

// The shape src/test/setup.ts stubs `location` with: a plain object, so BASE
// resolves to "" (direct/dev mode) for the statically-imported module above.
const DIRECT_LOCATION = { pathname: "/", protocol: "http:", host: "localhost" };

const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);

const ok = (data: unknown = {}) =>
  fetchMock.mockResolvedValue({ ok: true, json: async () => data } as unknown as Response);

const calledUrl = () => fetchMock.mock.calls[0][0] as string;
const calledInit = () => fetchMock.mock.calls[0][1] as RequestInit;

beforeEach(() => {
  fetchMock.mockReset();
});

// ---------------------------------------------------------------------------
// Endpoints with no test of their own
// ---------------------------------------------------------------------------

describe("uncovered endpoints", () => {
  it("copySchedule posts the target rooms to the schedule's copy sub-resource", async () => {
    ok([{ room_id: "room-3", schedule_id: "sched-9", status: "created" }]);
    const result = await api.copySchedule("room-1", "sched-1", ["room-2", "room-3"]);
    expect(calledUrl()).toBe("/api/rooms/room-1/schedules/sched-1/copy");
    expect(calledInit().method).toBe("POST");
    expect(calledInit().body).toBe(JSON.stringify({ target_room_ids: ["room-2", "room-3"] }));
    expect(result).toEqual([{ room_id: "room-3", schedule_id: "sched-9", status: "created" }]);
  });

  it("setMcpEnabled posts the flag to the MCP system endpoint", async () => {
    ok({ mcp_enabled: true });
    const result = await api.setMcpEnabled(true);
    expect(calledUrl()).toBe("/api/system/mcp");
    expect(calledInit().method).toBe("POST");
    expect(calledInit().body).toBe(JSON.stringify({ mcp_enabled: true }));
    expect(result).toEqual({ mcp_enabled: true });
  });

  it("getMqttStatus reads the bridge status settings endpoint (#519)", async () => {
    const status = {
      enabled: true,
      configured: true,
      connected: false,
      host: "broker.local",
      port: 1883,
      topic_prefix: "plenum",
      prefix_is_fallback: false,
      discovery: true,
      discovery_prefix: "homeassistant",
      last_error: "connection refused",
    };
    ok(status);
    expect(await api.getMqttStatus()).toEqual(status);
    expect(calledUrl()).toBe("/api/settings/mqtt");
    // A read must not carry a method override.
    expect(calledInit().method).toBeUndefined();
  });

  it("setMqttEnabled posts the flag to the MQTT system endpoint (#519)", async () => {
    ok({ mqtt_enabled: false });
    const result = await api.setMqttEnabled(false);
    expect(calledUrl()).toBe("/api/system/mqtt");
    expect(calledInit().method).toBe("POST");
    expect(calledInit().body).toBe(JSON.stringify({ mqtt_enabled: false }));
    expect(result).toEqual({ mqtt_enabled: false });
  });

  it("seedDemoMetrics posts an empty body to the dev-mode seeder (#442)", async () => {
    ok({
      seeded_cycles: 40,
      seeded_room_cycles: 80,
      seeded_events: 12,
      thermostats: 1,
      start_date: "2025-06-01",
      end_date: "2025-06-07",
    });
    const result = await api.seedDemoMetrics();
    expect(calledUrl()).toBe("/api/dev/seed-demo-metrics");
    expect(calledInit().method).toBe("POST");
    // The endpoint takes no arguments but is a POST, so it still needs a body.
    expect(calledInit().body).toBe("{}");
    expect(result.start_date).toBe("2025-06-01");
  });
});

// ---------------------------------------------------------------------------
// Query-string construction
// ---------------------------------------------------------------------------

describe("query-string construction", () => {
  it("getLogs sends until, and keeps an explicit zero limit/offset", async () => {
    ok([]);
    // `!= null`, not truthiness: limit 0 / offset 0 are meaningful and must
    // survive. A `if (params.limit)` guard would silently drop them.
    await api.getLogs({ limit: 0, offset: 0, until: "2024-03-02" });
    expect(calledUrl()).toBe("/api/logs?limit=0&offset=0&until=2024-03-02");
  });

  it("getLogs sends no query string at all when given no params", async () => {
    ok([]);
    await api.getLogs();
    expect(calledUrl()).toBe("/api/logs?");
  });

  it("getCycleTempSamples drops the '?' entirely when no room is given", async () => {
    ok([]);
    await api.getCycleTempSamples("cycle 1/2");
    // The id is URL-encoded, and with no room filter there is no query string.
    expect(calledUrl()).toBe("/api/logs/cycle%201%2F2/temp-samples");
  });

  it("getEventLogs sends every filter, including zero limit/offset", async () => {
    ok([]);
    await api.getEventLogs({
      limit: 0,
      offset: 0,
      category: "cycle",
      since: "2024-03-01",
      until: "2024-03-02",
      levels: ["warning"],
    });
    expect(calledUrl()).toBe(
      "/api/logs/events?limit=0&offset=0&category=cycle&since=2024-03-01&until=2024-03-02&level=warning"
    );
  });

  it("getEventLogs omits filters that were not supplied", async () => {
    ok([]);
    await api.getEventLogs({ levels: [] });
    // An empty level list is not a filter — `levels?.length` is falsy.
    expect(calledUrl()).toBe("/api/logs/events?");
  });

  it("getHAEntities accepts a single domain string as well as a list", async () => {
    ok([]);
    await api.getHAEntities("climate");
    expect(calledUrl()).toBe("/api/ha/entities?domain=climate");
  });

  it("getHAEntities forwards the exclude_icon filter", async () => {
    ok([]);
    await api.getHAEntities(["cover"], {
      hasAttribute: "current_position",
      excludeIcon: "mdi:fan",
    });
    expect(calledUrl()).toBe(
      "/api/ha/entities?domain=cover&has_attribute=current_position&exclude_icon=mdi%3Afan"
    );
  });

  it("getMetricsTimeseries forwards an end bound", async () => {
    ok({ series: [] });
    await api.getMetricsTimeseries("climate.test", "cycles", "day", {
      start: "2024-01-01",
      end: "2024-01-31",
    });
    expect(calledUrl()).toBe(
      "/api/metrics/thermostats/climate.test/timeseries?metric=cycles&granularity=day&start=2024-01-01&end=2024-01-31"
    );
  });

  it("getMetricsTimeseries defaults to day granularity and an unbounded range", async () => {
    ok({ series: [] });
    await api.getMetricsTimeseries("climate.test", "hours");
    expect(calledUrl()).toBe(
      "/api/metrics/thermostats/climate.test/timeseries?metric=hours&granularity=day"
    );
  });

  it("getMetricsEcoImpact hits the per-thermostat endpoint with a range", async () => {
    ok({ thermostat_entity_id: "climate.test" });
    await api.getMetricsEcoImpact("climate.test", { start: "2024-01-01", end: "2024-01-31" });
    expect(calledUrl()).toBe(
      "/api/metrics/thermostats/climate.test/eco-impact?start=2024-01-01&end=2024-01-31"
    );
  });

  it("getMetricsEcoImpact hits the home-wide endpoint for a null entity and no range", async () => {
    ok({ thermostat_entity_id: null });
    await api.getMetricsEcoImpact(null);
    // No entity id in the path, and the default empty range yields no "?".
    expect(calledUrl()).toBe("/api/metrics/thermostats/eco-impact");
  });
});

// ---------------------------------------------------------------------------
// Module-level base-path detection (HA ingress)
// ---------------------------------------------------------------------------
// BASE is computed once at import time from location.pathname, so these tests
// re-evaluate the module against a different location.

describe("ingress base path", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
    // Put the environment back the way src/test/setup.ts left it, so the
    // statically imported `api` above (and other files) keep working.
    vi.stubGlobal("location", DIRECT_LOCATION);
    vi.stubGlobal("fetch", fetchMock);
  });

  const loadApi = async (loc: Record<string, string>) => {
    vi.resetModules();
    vi.stubGlobal("location", loc);
    vi.stubGlobal("fetch", fetchMock);
    return import("./api");
  };

  it("prefixes every request with the ingress base path", async () => {
    const ingress = await loadApi({
      pathname: "/api/hassio_ingress/AbC-123_token/rooms",
      protocol: "https:",
      host: "ha.example.com",
    });
    ok([]);
    await ingress.getRooms();
    expect(calledUrl()).toBe("/api/hassio_ingress/AbC-123_token/api/rooms");
  });

  it("uses no prefix when the path only resembles the ingress prefix", async () => {
    const direct = await loadApi({
      pathname: "/api/hassio_ingressive/rooms",
      protocol: "http:",
      host: "localhost",
    });
    ok([]);
    await direct.getRooms();
    // The token segment is required — "/api/hassio_ingress" alone is not a match.
    expect(calledUrl()).toBe("/api/rooms");
  });

  it("falls back to no prefix when there is no location at all (non-browser host)", async () => {
    vi.resetModules();
    vi.stubGlobal("location", undefined);
    vi.stubGlobal("fetch", fetchMock);
    const headless = await import("./api");
    ok([]);
    await headless.getRooms();
    expect(calledUrl()).toBe("/api/rooms");
  });

  it("connectWS uses wss:// on an https page and carries the ingress prefix", async () => {
    const ingress = await loadApi({
      pathname: "/api/hassio_ingress/tok/",
      protocol: "https:",
      host: "ha.example.com",
    });
    const sockets: string[] = [];
    const socket = { addEventListener: vi.fn(), close: vi.fn() };
    vi.stubGlobal(
      "WebSocket",
      vi.fn(function (url: string) {
        sockets.push(url);
        return socket;
      })
    );
    const dispose = ingress.connectWS(() => {});
    expect(sockets).toEqual(["wss://ha.example.com/api/hassio_ingress/tok/ws"]);
    dispose();
    expect(socket.close).toHaveBeenCalled();
  });

  it("connectWS uses ws:// on a plain http page", async () => {
    const direct = await loadApi({ pathname: "/", protocol: "http:", host: "localhost:8099" });
    const sockets: string[] = [];
    const socket = { addEventListener: vi.fn(), close: vi.fn() };
    vi.stubGlobal(
      "WebSocket",
      vi.fn(function (url: string) {
        sockets.push(url);
        return socket;
      })
    );
    const dispose = direct.connectWS(() => {});
    expect(sockets).toEqual(["ws://localhost:8099/ws"]);
    dispose();
  });
});
