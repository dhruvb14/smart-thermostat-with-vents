import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import StaleSensorsBanner from "./StaleSensorsBanner";
import * as api from "../api";
import type { SensorHealth } from "../api";

vi.mock("../api");

const healthy: SensorHealth = { stale_after_min: 90, rooms: [] };

const room = (over: Partial<SensorHealth["rooms"][number]> = {}) => ({
  room_id: "room-1",
  room_name: "Guest Room",
  thermostat_entity_id: "climate.downstairs",
  stale_sensors: [],
  ...over,
});

describe("StaleSensorsBanner", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders nothing while all sensors are fresh", async () => {
    vi.mocked(api.getSensorHealth).mockResolvedValue(healthy);
    const { container } = render(<StaleSensorsBanner />);
    await waitFor(() => expect(api.getSensorHealth).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when the health fetch fails", async () => {
    vi.mocked(api.getSensorHealth).mockRejectedValue(new Error("network blip"));
    const { container } = render(<StaleSensorsBanner />);
    await waitFor(() => expect(api.getSensorHealth).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("lists stale sensors with the total count and threshold", async () => {
    vi.mocked(api.getSensorHealth).mockResolvedValue({
      stale_after_min: 90,
      rooms: [
        room({
          stale_sensors: [
            { entity_id: "sensor.guest_temp", age_seconds: 600, reason: "stale" },
            { entity_id: "sensor.guest_temp_2", age_seconds: null, reason: "not_in_cache" },
          ],
        }),
      ],
    });
    render(<StaleSensorsBanner />);
    const banner = await screen.findByTestId("stale-sensors-banner");
    expect(banner).toHaveTextContent("2 sensors not reporting");
    expect(banner).toHaveTextContent("threshold: 90 min");
    expect(banner).toHaveTextContent("Guest Room");
    // 600 s → minutes branch; never-seen branch for the uncached sensor.
    expect(banner).toHaveTextContent("10 min ago");
    expect(banner).toHaveTextContent("never seen by HA");
  });

  it("uses singular wording and the hours age format", async () => {
    vi.mocked(api.getSensorHealth).mockResolvedValue({
      stale_after_min: 60,
      rooms: [
        room({
          stale_sensors: [
            { entity_id: "sensor.guest_temp", age_seconds: 3 * 3600, reason: "stale" },
          ],
        }),
      ],
    });
    render(<StaleSensorsBanner />);
    const banner = await screen.findByTestId("stale-sensors-banner");
    expect(banner).toHaveTextContent("1 sensor not reporting");
    expect(banner).toHaveTextContent("3.0 h ago");
  });

  it("formats multi-day ages in days", async () => {
    vi.mocked(api.getSensorHealth).mockResolvedValue({
      stale_after_min: 60,
      rooms: [
        room({
          stale_sensors: [
            { entity_id: "sensor.guest_temp", age_seconds: 2 * 24 * 3600, reason: "stale" },
          ],
        }),
      ],
    });
    render(<StaleSensorsBanner />);
    expect(await screen.findByTestId("stale-sensors-banner")).toHaveTextContent("2 d ago");
  });
});
