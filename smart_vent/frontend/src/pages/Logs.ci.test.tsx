import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

// Both Logs tabs are pinned under CI so the golden screenshots stabilise
// (Issue #442, the Metrics-page pattern applied to Logs):
//
//  * the time window is pinned to CI_LOGS_RANGE — the seeded demo week — so the
//    feed and the cycle table render the REAL demo-flagged rows and live engine
//    rows (dated "now") fall outside the query, and
//  * the Live Feed starts PAUSED, because the initial fetch is deterministic but
//    websocket pushes would append between the update pass and the verify pass.
//
// Nothing else in the suite can see these branches: `ciPinned` reads a
// module-level `isCI` derived from VITE_APP_VERSION, which is not "CI" under
// vitest, so the ordinary Logs specs exercise only the live defaults ("1h" /
// "24h", unpaused, empty custom range). Stub the env and re-import with a fresh
// module registry, exactly as ci.test.tsx and Dashboard.ci.test.tsx do.

vi.mock("../api");

describe("Logs — CI build (#442)", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  const renderUnderCI = async () => {
    vi.resetModules();
    vi.stubEnv("VITE_APP_VERSION", "CI");

    const api = await import("../api");
    vi.mocked(api.getEventLogs).mockResolvedValue([]);
    vi.mocked(api.getLogs).mockResolvedValue([]);
    vi.mocked(api.getRooms).mockResolvedValue([]);
    vi.mocked(api.getThermostats).mockResolvedValue([]);
    vi.mocked(api.getLogRetention).mockResolvedValue({
      event_log_retention_days: 7,
      cycle_log_retention_days: 30,
    });
    vi.mocked(api.connectWS).mockReturnValue(() => {});

    const { CI_LOGS_RANGE } = await import("../ci");
    const Logs = (await import("./Logs")).default;
    render(<Logs />);
    return { api, CI_LOGS_RANGE };
  };

  it("starts the Live Feed paused so websocket pushes cannot move the golden", async () => {
    await renderUnderCI();
    // The button offers "Resume", which it only does while paused. Under a
    // normal build this reads "⏸ Pause".
    expect(await screen.findByRole("button", { name: /Resume/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /⏸ Pause/i })).not.toBeInTheDocument();
  });

  it("queries the Live Feed over the seeded demo week, not the rolling 1h window", async () => {
    const { api, CI_LOGS_RANGE } = await renderUnderCI();

    await waitFor(() => expect(api.getEventLogs).toHaveBeenCalled());
    const [params] = vi.mocked(api.getEventLogs).mock.calls[0];
    // The params object is optional in the signature; under CI the page must
    // always commit an explicit window, so its absence is itself a failure.
    if (!params)
      throw new Error("getEventLogs was called with no params — the CI window was not pinned");

    // Custom mode commits both bounds; the live default ("1h") would send a
    // `since` near now and no `until` at all.
    expect(params.since).toBe(new Date(CI_LOGS_RANGE.from).toISOString());
    expect(params.until).toBe(new Date(CI_LOGS_RANGE.to).toISOString());

    // The window is a fixed week in the past, so it can never coincide with a
    // rolling "last hour" range on any run date.
    expect(new Date(params.until as string).getTime()).toBeLessThan(Date.now());
  });

  it("pins the datetime-local inputs to the demo week", async () => {
    const { CI_LOGS_RANGE } = await renderUnderCI();

    await waitFor(() => {
      const inputs = document.querySelectorAll<HTMLInputElement>('input[type="datetime-local"]');
      // The feed's Custom range renders a from/to pair.
      expect(inputs.length).toBeGreaterThanOrEqual(2);
      expect(inputs[0].value).toBe(CI_LOGS_RANGE.from);
      expect(inputs[1].value).toBe(CI_LOGS_RANGE.to);
    });
  });

  it("pins the Cycle History window to the same demo week", async () => {
    const { api, CI_LOGS_RANGE } = await renderUnderCI();

    // Switch tabs; the history tab has its own independent pinned state.
    fireEvent.click(await screen.findByRole("button", { name: "Cycle History" }));

    await waitFor(() => expect(api.getLogs).toHaveBeenCalled());
    const [params] = vi.mocked(api.getLogs).mock.calls[0];
    // The params object is optional in the signature; under CI the page must
    // always commit an explicit window, so its absence is itself a failure.
    if (!params)
      throw new Error("getLogs was called with no params — the CI window was not pinned");
    expect(params.since).toBe(new Date(CI_LOGS_RANGE.from).toISOString());
    expect(params.until).toBe(new Date(CI_LOGS_RANGE.to).toISOString());
  });
});
