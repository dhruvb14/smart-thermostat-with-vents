import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import UnavailableThermostatsBanner from "./UnavailableThermostatsBanner";
import * as api from "../api";
import type { UnavailableThermostat } from "../api";

vi.mock("../api");

const thermostat = (over: Partial<UnavailableThermostat> = {}): UnavailableThermostat => ({
  thermostat_entity_id: "climate.downstairs",
  name: "Downstairs",
  reason: "unavailable",
  unavailable_seconds: 300,
  abort_after_min: 30,
  cycle_running: false,
  ...over,
});

describe("UnavailableThermostatsBanner", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders nothing while every thermostat is reachable", async () => {
    vi.mocked(api.getThermostatHealth).mockResolvedValue({ thermostats: [] });
    const { container } = render(<UnavailableThermostatsBanner />);
    await waitFor(() => expect(api.getThermostatHealth).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when the health fetch fails", async () => {
    vi.mocked(api.getThermostatHealth).mockRejectedValue(new Error("network blip"));
    const { container } = render(<UnavailableThermostatsBanner />);
    await waitFor(() => expect(api.getThermostatHealth).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("lists an unavailable thermostat with a minutes age", async () => {
    vi.mocked(api.getThermostatHealth).mockResolvedValue({
      thermostats: [thermostat({ unavailable_seconds: 300 })],
    });
    render(<UnavailableThermostatsBanner />);
    const banner = await screen.findByTestId("unavailable-thermostats-banner");
    expect(banner).toHaveTextContent("1 thermostat unavailable in Home Assistant");
    expect(banner).toHaveTextContent("Downstairs");
    expect(banner).toHaveTextContent("climate.downstairs");
    expect(banner).toHaveTextContent("unavailable for 5 min");
  });

  it("pluralises and covers the under-a-minute, hours, and never-seen ages", async () => {
    vi.mocked(api.getThermostatHealth).mockResolvedValue({
      thermostats: [
        thermostat({ unavailable_seconds: 10 }),
        thermostat({
          thermostat_entity_id: "climate.upstairs",
          name: "Upstairs",
          unavailable_seconds: 2 * 3600,
        }),
        thermostat({
          thermostat_entity_id: "climate.attic",
          name: "",
          reason: "not_in_cache",
          unavailable_seconds: null,
        }),
      ],
    });
    render(<UnavailableThermostatsBanner />);
    const banner = await screen.findByTestId("unavailable-thermostats-banner");
    expect(banner).toHaveTextContent("3 thermostats unavailable");
    expect(banner).toHaveTextContent("unavailable for under a minute");
    expect(banner).toHaveTextContent("unavailable for 2.0 h");
    expect(banner).toHaveTextContent("never seen by HA");
    // Falls back to the entity id when the name is empty — it appears twice
    // (as the title and as the code element).
    expect(banner.textContent?.match(/climate\.attic/g)?.length).toBe(2);
  });

  it("warns that a running cycle will auto-abort after the configured delay", async () => {
    vi.mocked(api.getThermostatHealth).mockResolvedValue({
      thermostats: [thermostat({ cycle_running: true, abort_after_min: 30 })],
    });
    render(<UnavailableThermostatsBanner />);
    expect(await screen.findByTestId("unavailable-thermostats-banner")).toHaveTextContent(
      "the running cycle aborts after 30 min and all vents re-open"
    );
  });

  it("warns loudly when auto-abort is disabled and a cycle is running", async () => {
    vi.mocked(api.getThermostatHealth).mockResolvedValue({
      thermostats: [thermostat({ cycle_running: true, abort_after_min: 0 })],
    });
    render(<UnavailableThermostatsBanner />);
    expect(await screen.findByTestId("unavailable-thermostats-banner")).toHaveTextContent(
      "will NOT be auto-aborted"
    );
  });
});
