import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import MqttBridgeCard from "./MqttBridgeCard";
import * as api from "../api";

vi.mock("../api");

const status = (over: Partial<api.MqttStatus> = {}): api.MqttStatus => ({
  enabled: false,
  configured: true,
  connected: false,
  host: "core-mosquitto",
  port: 1883,
  topic_prefix: "plenum",
  prefix_is_fallback: false,
  discovery: true,
  discovery_prefix: "homeassistant",
  last_error: null,
  ...over,
});

const renderCard = async (over: Partial<api.MqttStatus> = {}) => {
  vi.mocked(api.getMqttStatus).mockResolvedValue(status(over));
  const view = render(<MqttBridgeCard />);
  await screen.findByText("MQTT bridge");
  return view;
};

describe("MqttBridgeCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.setMqttEnabled).mockResolvedValue({ mqtt_enabled: true });
  });

  it("renders nothing until the status has loaded", () => {
    vi.mocked(api.getMqttStatus).mockReturnValue(new Promise(() => {}));
    const { container } = render(<MqttBridgeCard />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows Off when the bridge is disabled", async () => {
    await renderCard({ enabled: false });
    expect(screen.getByText("Off")).toBeInTheDocument();
  });

  it("shows Connected when enabled and connected", async () => {
    await renderCard({ enabled: true, connected: true });
    expect(screen.getByText("Connected")).toBeInTheDocument();
  });

  it("distinguishes enabled-but-not-yet-connected", async () => {
    // A broker that is down must not read the same as a bridge that is off.
    await renderCard({ enabled: true, connected: false });
    expect(screen.getByText("Connecting…")).toBeInTheDocument();
  });

  it("turns the bridge on", async () => {
    await renderCard({ enabled: false });
    fireEvent.click(screen.getByRole("button", { name: "Turn on MQTT bridge" }));
    await waitFor(() => expect(api.setMqttEnabled).toHaveBeenCalledWith(true));
  });

  it("turns the bridge off", async () => {
    await renderCard({ enabled: true, connected: true });
    fireEvent.click(screen.getByRole("button", { name: "Turn off MQTT bridge" }));
    await waitFor(() => expect(api.setMqttEnabled).toHaveBeenCalledWith(false));
  });

  it("cannot be turned on with no broker configured", async () => {
    await renderCard({ configured: false });
    expect(screen.getByRole("button", { name: "Turn on MQTT bridge" })).toBeDisabled();
    expect(screen.getByText(/No MQTT broker was found/i)).toBeInTheDocument();
  });

  it("reports the resolved broker and topic prefix", async () => {
    // The prefix is derived from the add-on slug, so this panel is the only
    // place a user can discover what it actually resolved to.
    await renderCard({ topic_prefix: "plenum_beta" });
    expect(screen.getByText("core-mosquitto:1883")).toBeInTheDocument();
    expect(screen.getByText("plenum_beta/")).toBeInTheDocument();
  });

  it("warns when the prefix could collide with another install", async () => {
    await renderCard({ prefix_is_fallback: true });
    expect(screen.getByText(/would collide/i)).toBeInTheDocument();
  });

  it("does not warn about collisions with a slug-derived prefix", async () => {
    await renderCard({ prefix_is_fallback: false });
    expect(screen.queryByText(/would collide/i)).not.toBeInTheDocument();
  });

  it("explains discovery when it is off", async () => {
    await renderCard({ discovery: false });
    expect(screen.getByText(/no entities are created/i)).toBeInTheDocument();
  });

  it("surfaces the last connection error while disconnected", async () => {
    await renderCard({
      enabled: true,
      connected: false,
      last_error: "OSError: broker unreachable",
    });
    expect(screen.getByText(/broker unreachable/i)).toBeInTheDocument();
  });

  it("hides the last error once connected", async () => {
    await renderCard({ enabled: true, connected: true, last_error: "stale" });
    expect(screen.queryByText("stale")).not.toBeInTheDocument();
  });

  it("says that safety settings are not exposed over MQTT", async () => {
    // #519's security decision, surfaced to the user rather than only living in
    // the code.
    await renderCard();
    expect(screen.getByText(/safety settings/i)).toBeInTheDocument();
  });

  it("survives a failing status fetch", async () => {
    vi.mocked(api.getMqttStatus).mockRejectedValue(new Error("nope"));
    const { container } = render(<MqttBridgeCard />);
    await waitFor(() => expect(api.getMqttStatus).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
