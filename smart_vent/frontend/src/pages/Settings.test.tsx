import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import Settings from "./Settings";
import * as api from "../api";
import { McpContext, AuthContext, type AuthContextValue } from "../contexts";

vi.mock("../api");

const authValue = (over: Partial<AuthContextValue> = {}): AuthContextValue => ({
  requireAuth: false,
  method: "open",
  logout: vi.fn(),
  ...over,
});

function renderSettings({
  mcpEnabled = false,
  toggleMcp = vi.fn().mockResolvedValue(undefined),
  auth = authValue(),
}: {
  mcpEnabled?: boolean;
  toggleMcp?: () => Promise<void>;
  auth?: AuthContextValue;
} = {}) {
  return render(
    <AuthContext.Provider value={auth}>
      <McpContext.Provider value={{ mcpEnabled, toggleMcp }}>
        <Settings />
      </McpContext.Provider>
    </AuthContext.Provider>
  );
}

describe("Settings page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listMcpTokens).mockResolvedValue([]);
    vi.mocked(api.downloadBackup).mockReturnValue(undefined);
  });

  it("renders the MCP server card, inline setup guidance, and Backup & Restore", () => {
    renderSettings();
    expect(screen.getByText(/Settings/i, { selector: ".page-title" })).toBeInTheDocument();
    expect(screen.getByText("MCP server")).toBeInTheDocument();
    expect(screen.getByText(/Backup & Restore/i)).toBeInTheDocument();
    // The setup steps render inline (readable) instead of being buried in the modal.
    expect(screen.getByText(/Home Assistant OS \/ Supervised/i)).toBeInTheDocument();
    expect(screen.getByText(/Docker \(standalone\)/i)).toBeInTheDocument();
  });

  it("hides the token card and shows the unauthenticated note when auth is off", () => {
    renderSettings({ auth: authValue({ requireAuth: false }) });
    expect(screen.queryByText(/MCP access tokens/i)).not.toBeInTheDocument();
    expect(screen.getByText(/unauthenticated/i)).toBeInTheDocument();
  });

  it("shows the token card and the bearer-token note when auth is required", async () => {
    renderSettings({ auth: authValue({ requireAuth: true }) });
    expect(await screen.findByText(/MCP access tokens/i)).toBeInTheDocument();
    expect(screen.getByText(/mint a bearer token below/i)).toBeInTheDocument();
  });

  it("confirms enabling the MCP server", async () => {
    const toggleMcp = vi.fn().mockResolvedValue(undefined);
    renderSettings({ mcpEnabled: false, toggleMcp });
    expect(screen.getByText("Off")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Turn on/i }));
    expect(await screen.findByText(/Turn on MCP server\?/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Confirm/i }));

    await waitFor(() => expect(toggleMcp).toHaveBeenCalled());
  });

  it("notes that a token is needed when enabling with auth on", async () => {
    renderSettings({ mcpEnabled: false, auth: authValue({ requireAuth: true }) });
    fireEvent.click(screen.getByRole("button", { name: /Turn on/i }));
    expect(await screen.findByText(/mint a bearer token after enabling/i)).toBeInTheDocument();
  });

  it("cancels disabling the MCP server without toggling", async () => {
    const toggleMcp = vi.fn();
    renderSettings({ mcpEnabled: true, toggleMcp });
    expect(screen.getByText("Running")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Turn off/i }));
    expect(await screen.findByText(/Turn off MCP server\?/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Cancel/i }));

    expect(toggleMcp).not.toHaveBeenCalled();
  });

  it("downloads a backup", () => {
    renderSettings();
    fireEvent.click(screen.getByText("Download backup"));
    expect(api.downloadBackup).toHaveBeenCalled();
  });

  it("restores from a backup file", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.mocked(api.restoreBackup).mockResolvedValue(undefined);
    const { container } = renderSettings();

    const fileInput = container.querySelector("#restore-backup-input") as HTMLInputElement;
    const file = new File(["x"], "plenum.db", { type: "application/x-sqlite3" });
    fireEvent.change(fileInput, { target: { files: [file] } });

    await waitFor(() => expect(api.restoreBackup).toHaveBeenCalledWith(file));
    expect(await screen.findByText(/Restore complete/i)).toBeInTheDocument();
  });

  it("aborts a restore when the user cancels the confirm prompt", () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const { container } = renderSettings();

    const fileInput = container.querySelector("#restore-backup-input") as HTMLInputElement;
    const file = new File(["x"], "plenum.db", { type: "application/x-sqlite3" });
    fireEvent.change(fileInput, { target: { files: [file] } });

    expect(api.restoreBackup).not.toHaveBeenCalled();
  });
});
