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

function renderSettings() {
  return render(
    <AuthContext.Provider value={authValue()}>
      <McpContext.Provider
        value={{ mcpEnabled: false, toggleMcp: vi.fn().mockResolvedValue(undefined) }}
      >
        <Settings />
      </McpContext.Provider>
    </AuthContext.Provider>
  );
}

const dbFile = () => new File(["x"], "plenum.db", { type: "application/x-sqlite3" });

describe("Settings — uncovered branches", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listMcpTokens).mockResolvedValue([]);
    vi.mocked(api.downloadBackup).mockReturnValue(undefined);
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
  });

  // ── Empty file selection (line 20) ────────────────────────────────────────

  it("does nothing when the file picker is dismissed without a selection", () => {
    // Cancelling the native picker fires a change event with an empty FileList.
    // That must not prompt for confirmation nor hit the restore endpoint.
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const { container } = renderSettings();

    const fileInput = container.querySelector("#restore-backup-input") as HTMLInputElement;
    fireEvent.change(fileInput, { target: { files: [] } });

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(api.restoreBackup).not.toHaveBeenCalled();
    // No status badge appears — the card is untouched.
    expect(screen.queryByText(/Restore complete/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Restore failed/i)).not.toBeInTheDocument();
  });

  // ── Non-Error rejection (line 33) ─────────────────────────────────────────

  it("shows a generic failure badge when the restore rejects with a non-Error", async () => {
    // An aborted upload can reject with a bare value rather than an Error; the
    // badge must still carry readable copy.
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.mocked(api.restoreBackup).mockRejectedValue("upload aborted");
    const { container } = renderSettings();

    const fileInput = container.querySelector("#restore-backup-input") as HTMLInputElement;
    fireEvent.change(fileInput, { target: { files: [dbFile()] } });

    const badge = await screen.findByText("Restore failed");
    expect(badge.className).toContain("badge-red");
    // The button comes back out of the "Restoring…" state so a retry is possible.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Restore from backup/i })).toBeEnabled()
    );
  });
});
