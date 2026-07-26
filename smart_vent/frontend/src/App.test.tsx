import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import App from "./App";
import * as api from "./api";
import * as contexts from "./contexts";
import { MemoryRouter } from "react-router-dom";

vi.mock("./api");

describe("App Root", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getSensorHealth).mockResolvedValue({ stale_after_min: 30, rooms: [] });
    vi.mocked(api.getSensorStaleness).mockResolvedValue({ stale_after_min: 30 });
    vi.mocked(api.getThermostatHealth).mockResolvedValue({ thermostats: [] });
    vi.mocked(api.getSystemStatus).mockResolvedValue({ enabled: true, dev_mode: false });
    // AuthGate reads this on mount; default to auth-off so the app renders.
    vi.mocked(api.getAuthStatus).mockResolvedValue({
      require_auth: false,
      authenticated: true,
      method: "open",
    });
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "F",
      theme: "system",
      unit_change_ack_required: false,
      vacation_mode: { enabled: false, return_at: null },
      eco_suspend: {},
    });
    // The Settings page's MQTT card polls this on mount (#519).
    vi.mocked(api.getMqttStatus).mockResolvedValue({
      enabled: false,
      configured: false,
      connected: false,
      host: null,
      port: null,
      topic_prefix: "plenum",
      prefix_is_fallback: false,
      discovery: true,
      discovery_prefix: "homeassistant",
      last_error: null,
    });
    vi.mocked(api.connectWS).mockReturnValue(() => {});
    vi.mocked(api.getStatus).mockResolvedValue([]);
    vi.mocked(api.getRooms).mockResolvedValue([]);
    vi.mocked(api.getThermostats).mockResolvedValue([]);
    vi.mocked(api.getVacationMode).mockResolvedValue({ enabled: false, return_at: null });
  });

  it("renders the app and navigates", async () => {
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </MemoryRouter>
    );

    expect(await screen.findByText(/Dashboard/i, { selector: ".page-title" })).toBeInTheDocument();

    // Navigate to Rooms
    const roomsLink = screen.getAllByText(/Rooms/i)[0]; // Link in nav
    fireEvent.click(roomsLink);
    expect(await screen.findByText(/Rooms/i, { selector: ".page-title" })).toBeInTheDocument();
  });

  it("memoizes the unit context so a system toggle does not rebuild it (Issue #293)", async () => {
    vi.mocked(api.setSystemEnabled).mockResolvedValue({ enabled: false });
    const buildSpy = vi.spyOn(contexts, "buildUnitContext");
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </MemoryRouter>
    );
    await screen.findByLabelText(/Settings/i);
    // Sanity: the spy actually captures AppRoot's calls.
    await waitFor(() => expect(buildSpy).toHaveBeenCalled());
    const before = buildSpy.mock.calls.length;

    // Toggle System Off — re-renders AppRoot (toggling, then enabled) but leaves
    // the unit unchanged. The memoized context must NOT be rebuilt.
    fireEvent.click(screen.getByLabelText(/Settings/i));
    fireEvent.click(await screen.findByText(/System On/i));
    fireEvent.click(screen.getByRole("button", { name: /Confirm/i }));
    await waitFor(() => expect(api.setSystemEnabled).toHaveBeenCalled());
    // Re-open the menu to confirm the toggle settled (forces the post-toggle
    // re-renders to have flushed).
    fireEvent.click(screen.getByLabelText(/Settings/i));
    expect(await screen.findByText(/System Off/i)).toBeInTheDocument();

    expect(buildSpy.mock.calls.length).toBe(before);
    buildSpy.mockRestore();
  });

  it("toggles system status via dropdown and confirmation", async () => {
    vi.mocked(api.setSystemEnabled).mockResolvedValue({ enabled: false });
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </MemoryRouter>
    );

    // Open settings dropdown
    const gearBtn = await screen.findByLabelText(/Settings/i);
    fireEvent.click(gearBtn);

    // Click the system item in the dropdown
    const systemItem = await screen.findByText(/System On/i);
    fireEvent.click(systemItem);

    // Confirmation dialog appears
    expect(await screen.findByText(/Turn off system\?/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Confirm/i }));

    await waitFor(() => {
      expect(api.setSystemEnabled).toHaveBeenCalledWith(false);
    });

    // Re-open dropdown to confirm state updated
    fireEvent.click(gearBtn);
    expect(await screen.findByText(/System Off/i)).toBeInTheDocument();
  });

  it("cancels system toggle when cancel is clicked", async () => {
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </MemoryRouter>
    );

    const gearBtn = await screen.findByLabelText(/Settings/i);
    fireEvent.click(gearBtn);
    fireEvent.click(await screen.findByText(/System On/i));
    expect(await screen.findByText(/Turn off system\?/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Cancel/i }));

    expect(api.setSystemEnabled).not.toHaveBeenCalled();
  });

  it("toggles dev mode via dropdown and confirmation", async () => {
    vi.mocked(api.setDevModeApi).mockResolvedValue({ dev_mode: true });
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </MemoryRouter>
    );

    // Open settings dropdown
    const gearBtn = await screen.findByLabelText(/Settings/i);
    fireEvent.click(gearBtn);

    // Click the dev mode item in the dropdown
    const devItem = await screen.findByText(/Dev Off/i);
    fireEvent.click(devItem);

    // Confirmation dialog appears
    expect(await screen.findByText(/Developer Mode/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Confirm/i }));

    await waitFor(() => {
      expect(api.setDevModeApi).toHaveBeenCalledWith(true);
    });

    // Re-open dropdown to confirm state updated
    fireEvent.click(gearBtn);
    expect(await screen.findByText(/Dev On/i)).toBeInTheDocument();
  });

  it("cancels dev mode toggle when cancel is clicked", async () => {
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </MemoryRouter>
    );

    const gearBtn = await screen.findByLabelText(/Settings/i);
    fireEvent.click(gearBtn);
    fireEvent.click(await screen.findByText(/Dev Off/i));
    expect(await screen.findByText(/Developer Mode/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Cancel/i }));

    expect(api.setDevModeApi).not.toHaveBeenCalled();
  });

  it("toggles the MCP server from the Settings page and confirmation", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue({
      enabled: true,
      dev_mode: false,
      mcp_enabled: false,
    });
    vi.mocked(api.setMcpEnabled).mockResolvedValue({ mcp_enabled: true });
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </MemoryRouter>
    );

    // The MCP toggle moved from the gear dropdown to the Settings page (#471).
    fireEvent.click(await screen.findByRole("link", { name: /Settings/i }));
    expect(await screen.findByText(/Settings/i, { selector: ".page-title" })).toBeInTheDocument();

    // Enabling opens a short, readable confirmation modal.
    fireEvent.click(await screen.findByRole("button", { name: "Turn on" }));
    expect(await screen.findByText(/Turn on MCP server\?/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Confirm/i }));

    await waitFor(() => {
      expect(api.setMcpEnabled).toHaveBeenCalledWith(true);
    });

    // The card reflects the running state.
    expect(await screen.findByText("Running")).toBeInTheDocument();
  });

  it("applies a persisted dark theme to <html data-theme> on load", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "F",
      theme: "dark",
      unit_change_ack_required: false,
      vacation_mode: { enabled: false, return_at: null },
      eco_suspend: {},
    });
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </MemoryRouter>
    );
    await waitFor(() => {
      expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    });
  });

  it("cycles the theme via the settings dropdown and persists each step", async () => {
    vi.mocked(api.setThemeApi).mockImplementation(async (theme) => ({ theme }));
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </MemoryRouter>
    );

    const gearBtn = await screen.findByLabelText(/Settings/i);
    fireEvent.click(gearBtn);

    // The theme item keeps the menu open (its label shows the new state), so
    // the cycle can be clicked through without reopening the dropdown.
    // Default is System (no data-theme attribute → prefers-color-scheme rules).
    fireEvent.click(await screen.findByText(/Theme: System/i));
    await waitFor(() => expect(api.setThemeApi).toHaveBeenCalledWith("light"));
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");

    fireEvent.click(await screen.findByText(/Theme: Light/i));
    await waitFor(() => expect(api.setThemeApi).toHaveBeenCalledWith("dark"));
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");

    fireEvent.click(await screen.findByText(/Theme: Dark/i));
    await waitFor(() => expect(api.setThemeApi).toHaveBeenCalledWith("system"));
    // System clears the attribute so the OS preference takes over again.
    expect(document.documentElement.getAttribute("data-theme")).toBeNull();
  });

  it("cancels the MCP toggle from the Settings page when cancel is clicked", async () => {
    vi.mocked(api.getSystemStatus).mockResolvedValue({
      enabled: true,
      dev_mode: false,
      mcp_enabled: false,
    });
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </MemoryRouter>
    );

    fireEvent.click(await screen.findByRole("link", { name: /Settings/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Turn on" }));
    expect(await screen.findByText(/Turn on MCP server\?/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Cancel/i }));

    expect(api.setMcpEnabled).not.toHaveBeenCalled();
  });

  // --- Auth gate (#373) ---

  const renderApp = () =>
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </MemoryRouter>
    );

  it("applies system/dev/theme websocket pushes from other sessions", async () => {
    const callbacks: Array<(event: { type: string; data: Record<string, unknown> }) => void> = [];
    vi.mocked(api.connectWS).mockImplementation((cb) => {
      callbacks.push(cb as (typeof callbacks)[number]);
      return () => {};
    });
    renderApp();
    const gearBtn = await screen.findByLabelText(/Settings/i);

    const push = (type: string, data: Record<string, unknown>) =>
      callbacks.forEach((cb) => cb({ type, data }));

    await waitFor(() => expect(callbacks.length).toBeGreaterThan(0));
    push("system_enabled_changed", { enabled: false });
    push("dev_mode_changed", { dev_mode: true });
    push("mcp_enabled_changed", { mcp_enabled: true });
    push("theme_changed", { theme: "dark" });

    // Dev mode on → the Dev Mode nav link appears without any local toggle.
    expect(await screen.findByText(/Dev Mode/i)).toBeInTheDocument();
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("dark"));
    fireEvent.click(gearBtn);
    expect(await screen.findByText(/System Off/i)).toBeInTheDocument();
    expect(screen.getByText(/Dev On/i)).toBeInTheDocument();
  });

  it("closes the settings dropdown on an outside click", async () => {
    renderApp();
    fireEvent.click(await screen.findByLabelText(/Settings/i));
    expect(await screen.findByText(/System On/i)).toBeInTheDocument();
    fireEvent.mouseDown(document.body);
    await waitFor(() => expect(screen.queryByText(/System On/i)).not.toBeInTheDocument());
  });

  it("navigates to the self-hosted API docs from the dropdown", async () => {
    renderApp();
    fireEvent.click(await screen.findByLabelText(/Settings/i));
    fireEvent.click(await screen.findByText(/API Docs/i));
    // The click handler bypasses the router and does a full navigation.
    expect((window.location as { href?: string }).href).toBe("api/docs/");
  });

  it("opens and closes the mobile menu via the hamburger", async () => {
    const { container } = renderApp();
    await screen.findByLabelText(/Settings/i);
    fireEvent.click(screen.getByLabelText(/Toggle menu/i));
    const mobileMenu = container.querySelector(".nav-mobile-menu");
    expect(mobileMenu).not.toBeNull();
    // Clicking a link inside the menu closes it.
    fireEvent.click(within(mobileMenu as HTMLElement).getByText("Rooms"));
    await waitFor(() => expect(container.querySelector(".nav-mobile-menu")).toBeNull());
    expect(await screen.findByText(/Rooms/i, { selector: ".page-title" })).toBeInTheDocument();
  });

  it("shows the ingress signed-in status without a Log out control", async () => {
    vi.mocked(api.getAuthStatus).mockResolvedValue({
      require_auth: true,
      authenticated: true,
      method: "ingress",
    });
    renderApp();
    fireEvent.click(await screen.findByLabelText(/Settings/i));
    expect(await screen.findByText(/Signed in via Home Assistant/i)).toBeInTheDocument();
    expect(screen.queryByText(/Log out/i)).not.toBeInTheDocument();
  });

  it("shows the login screen when require_auth is on and not authenticated", async () => {
    vi.mocked(api.getAuthStatus).mockResolvedValue({
      require_auth: true,
      authenticated: false,
      method: "none",
    });
    renderApp();
    expect(await screen.findByRole("button", { name: /Sign in/i })).toBeInTheDocument();
    // The app itself (nav) is not rendered behind the gate.
    expect(screen.queryByLabelText(/Settings/i)).not.toBeInTheDocument();
  });

  it("renders the app (not login) for an authenticated ingress caller", async () => {
    vi.mocked(api.getAuthStatus).mockResolvedValue({
      require_auth: true,
      authenticated: true,
      method: "ingress",
    });
    renderApp();
    expect(await screen.findByText(/Dashboard/i, { selector: ".page-title" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Sign in$/i })).not.toBeInTheDocument();
  });

  it("shows a Log out control only for a session-authenticated user", async () => {
    vi.mocked(api.getAuthStatus).mockResolvedValue({
      require_auth: true,
      authenticated: true,
      method: "session",
    });
    vi.mocked(api.logout).mockResolvedValue({ ok: true });
    renderApp();
    fireEvent.click(await screen.findByLabelText(/Settings/i));
    fireEvent.click(await screen.findByText(/Log out/i));
    await waitFor(() => expect(api.logout).toHaveBeenCalled());
  });

  it("logs in from the gate and then renders the app", async () => {
    // First probe: not authenticated → login. After a successful login the gate
    // re-checks and gets an authenticated status → the app renders.
    vi.mocked(api.getAuthStatus)
      .mockResolvedValueOnce({ require_auth: true, authenticated: false, method: "none" })
      .mockResolvedValue({ require_auth: true, authenticated: true, method: "session" });
    vi.mocked(api.login).mockResolvedValue({ ok: true });
    renderApp();

    fireEvent.change(await screen.findByLabelText(/Username/i), { target: { value: "a" } });
    fireEvent.change(screen.getByLabelText(/Password/i), { target: { value: "b" } });
    fireEvent.click(screen.getByRole("button", { name: /Sign in/i }));

    expect(await screen.findByText(/Dashboard/i, { selector: ".page-title" })).toBeInTheDocument();
    expect(api.login).toHaveBeenCalledWith("a", "b");
  });
});
