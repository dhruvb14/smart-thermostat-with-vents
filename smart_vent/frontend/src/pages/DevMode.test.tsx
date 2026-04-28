import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import DevMode from "./DevMode";
import * as api from "../api";

import { DevModeContext } from "../contexts";

vi.mock("../api");

const mockDevLogs: api.EventLogEntry[] = [
  {
    id: 1,
    timestamp: "2024-01-01T12:00:00",
    message: "Vent opened",
    level: "info", category: "dev",
    details: { action: "open_vent", entity_id: "cover.living_room" }
  }
];

describe("DevMode Page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.getDevLogs as any).mockResolvedValue(mockDevLogs);
    (api.getStatus as any).mockResolvedValue([]);
    (api.getSystemStatus as any).mockResolvedValue({ enabled: true, dev_mode: true });
    (api.connectWS as any).mockReturnValue(() => {});
  });

  it("renders the dev mode page when devMode is enabled", async () => {
    render(
      <DevModeContext.Provider value={{ devMode: true, toggleDevMode: async () => {} }}>
        <DevMode />
      </DevModeContext.Provider>
    );
    expect(await screen.findByText(/🛠 Developer Mode/i)).toBeInTheDocument();
    expect(await screen.findByText(/Vent opened/i)).toBeInTheDocument();
  });

  it("renders restricted message when devMode is disabled", async () => {
    render(
      <DevModeContext.Provider value={{ devMode: false, toggleDevMode: async () => {} }}>
        <DevMode />
      </DevModeContext.Provider>
    );
    expect(screen.getByText(/Developer mode is off/i)).toBeInTheDocument();
  });

  it("allows clearing logs", async () => {
    render(
      <DevModeContext.Provider value={{ devMode: true, toggleDevMode: async () => {} }}>
        <DevMode />
      </DevModeContext.Provider>
    );

    expect(await screen.findByText(/Vent opened/i)).toBeInTheDocument();
    const clearBtn = screen.getByText("Clear");
    fireEvent.click(clearBtn);
    expect(screen.queryByText(/Vent opened/i)).not.toBeInTheDocument();
  });
});
