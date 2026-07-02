import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "F",
      unit_change_ack_required: false,
      vacation_mode: { enabled: false, return_at: null },
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

  it("toggles the MCP server via dropdown and confirmation", async () => {
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

    const gearBtn = await screen.findByLabelText(/Settings/i);
    fireEvent.click(gearBtn);

    // Menu shows "MCP Off" initially
    fireEvent.click(await screen.findByText(/MCP Off/i));

    // Confirmation dialog explains the attach URL
    expect(await screen.findByText(/Turn on MCP server\?/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Confirm/i }));

    await waitFor(() => {
      expect(api.setMcpEnabled).toHaveBeenCalledWith(true);
    });

    // Re-open dropdown to confirm state updated
    fireEvent.click(gearBtn);
    expect(await screen.findByText(/MCP On/i)).toBeInTheDocument();
  });

  it("cancels the MCP toggle when cancel is clicked", async () => {
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

    const gearBtn = await screen.findByLabelText(/Settings/i);
    fireEvent.click(gearBtn);
    fireEvent.click(await screen.findByText(/MCP Off/i));
    expect(await screen.findByText(/Turn on MCP server\?/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Cancel/i }));

    expect(api.setMcpEnabled).not.toHaveBeenCalled();
  });
});
