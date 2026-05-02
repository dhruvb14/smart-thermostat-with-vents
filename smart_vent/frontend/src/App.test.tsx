import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import App from "./App";
import * as api from "./api";
import { MemoryRouter } from "react-router-dom";

vi.mock("./api");

describe("App Root", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getSystemStatus).mockResolvedValue({ enabled: true, dev_mode: false });
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "F",
      unit_change_ack_required: false,
    });
    vi.mocked(api.connectWS).mockReturnValue(() => {});
    vi.mocked(api.getStatus).mockResolvedValue([]);
    vi.mocked(api.getRooms).mockResolvedValue([]);
    vi.mocked(api.getThermostats).mockResolvedValue([]);
  });

  it("renders the app and navigates", async () => {
    render(
      <MemoryRouter>
        <App />
      </MemoryRouter>
    );

    expect(await screen.findByText(/Dashboard/i, { selector: ".page-title" })).toBeInTheDocument();

    // Navigate to Rooms
    const roomsLink = screen.getAllByText(/Rooms/i)[0]; // Link in nav
    fireEvent.click(roomsLink);
    expect(await screen.findByText(/Rooms/i, { selector: ".page-title" })).toBeInTheDocument();
  });

  it("toggles system status", async () => {
    vi.mocked(api.setSystemEnabled).mockResolvedValue({ enabled: false });
    render(
      <MemoryRouter>
        <App />
      </MemoryRouter>
    );

    const toggleBtn = await screen.findByText(/System On/i);
    fireEvent.click(toggleBtn);

    await waitFor(() => {
      expect(api.setSystemEnabled).toHaveBeenCalledWith(false);
    });
    expect(await screen.findByText(/System Off/i)).toBeInTheDocument();
  });

  it("toggles dev mode", async () => {
    vi.mocked(api.setDevModeApi).mockResolvedValue({ dev_mode: true });
    render(
      <MemoryRouter>
        <App />
      </MemoryRouter>
    );

    const devBtn = await screen.findByText(/Dev Off/i);
    fireEvent.click(devBtn);

    await waitFor(() => {
      expect(api.setDevModeApi).toHaveBeenCalledWith(true);
    });
    expect(await screen.findByText(/Dev On/i)).toBeInTheDocument();
  });
});
