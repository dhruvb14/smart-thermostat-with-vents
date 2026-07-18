import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import McpTokensCard from "./McpTokensCard";
import * as api from "../api";

vi.mock("../api");

describe("McpTokensCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listMcpTokens).mockResolvedValue([]);
  });

  it("lists existing tokens (metadata only, with a revoke button)", async () => {
    vi.mocked(api.listMcpTokens).mockResolvedValue([
      { id: "t1", label: "Claude", scope: "read", created_at: "2025-01-01", last_used_at: null },
    ]);
    render(<McpTokensCard />);
    expect(await screen.findByText("Claude")).toBeInTheDocument();
    // The scope badge (distinct from the <select> option of the same text).
    expect(screen.getByText("read", { selector: "span.badge" })).toBeInTheDocument();
    expect(screen.getByText(/Last used: never/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Revoke/i })).toBeInTheDocument();
  });

  it("mints a token and shows the secret once", async () => {
    vi.mocked(api.mintMcpToken).mockResolvedValue({
      id: "t2",
      label: "New",
      scope: "write",
      created_at: "x",
      last_used_at: null,
      token: "super-secret-value",
    });
    render(<McpTokensCard />);
    fireEvent.change(await screen.findByLabelText(/Label/i), { target: { value: "New" } });
    fireEvent.change(screen.getByLabelText(/Scope/i), { target: { value: "write" } });
    fireEvent.click(screen.getByRole("button", { name: /Mint token/i }));

    await waitFor(() => expect(api.mintMcpToken).toHaveBeenCalledWith("New", "write"));
    expect(await screen.findByText("super-secret-value")).toBeInTheDocument();
    // Dismiss the one-time secret box.
    fireEvent.click(screen.getByRole("button", { name: /Done/i }));
    await waitFor(() => expect(screen.queryByText("super-secret-value")).not.toBeInTheDocument());
  });

  it("disables mint until a label is entered", async () => {
    render(<McpTokensCard />);
    const btn = await screen.findByRole("button", { name: /Mint token/i });
    expect(btn).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Label/i), { target: { value: "x" } });
    expect(btn).not.toBeDisabled();
  });

  it("asks for confirmation before revoking, naming the token", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.mocked(api.listMcpTokens).mockResolvedValue([
      { id: "t1", label: "Old", scope: "read", created_at: "x", last_used_at: null },
    ]);
    vi.mocked(api.revokeMcpToken).mockResolvedValue({ deleted: true });
    render(<McpTokensCard />);
    fireEvent.click(await screen.findByRole("button", { name: /Revoke/i }));

    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining('"Old"'));
    await waitFor(() => expect(api.revokeMcpToken).toHaveBeenCalledWith("t1"));
  });

  it("does not revoke when the user cancels the confirmation", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    vi.mocked(api.listMcpTokens).mockResolvedValue([
      { id: "t1", label: "Old", scope: "read", created_at: "x", last_used_at: null },
    ]);
    render(<McpTokensCard />);
    fireEvent.click(await screen.findByRole("button", { name: /Revoke/i }));

    expect(window.confirm).toHaveBeenCalled();
    expect(api.revokeMcpToken).not.toHaveBeenCalled();
  });

  it("shows an error when minting fails", async () => {
    vi.mocked(api.mintMcpToken).mockRejectedValue(new Error("scope must be one of ..."));
    render(<McpTokensCard />);
    fireEvent.change(await screen.findByLabelText(/Label/i), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: /Mint token/i }));
    expect(await screen.findByText(/scope must be one of/i)).toBeInTheDocument();
  });

  it("edits a token's scope", async () => {
    vi.mocked(api.listMcpTokens).mockResolvedValue([
      { id: "t1", label: "Old", scope: "read", created_at: "x", last_used_at: null },
    ]);
    vi.mocked(api.updateMcpTokenScope).mockResolvedValue({
      id: "t1",
      label: "Old",
      scope: "write",
      created_at: "x",
      last_used_at: null,
    });
    render(<McpTokensCard />);
    fireEvent.click(await screen.findByRole("button", { name: /Edit/i }));

    const select = screen.getByLabelText(/Scope for Old/i) as HTMLSelectElement;
    expect(select.value).toBe("read");
    fireEvent.change(select, { target: { value: "write" } });
    fireEvent.click(screen.getByRole("button", { name: /Save/i }));

    await waitFor(() => expect(api.updateMcpTokenScope).toHaveBeenCalledWith("t1", "write"));
    await waitFor(() => expect(api.listMcpTokens).toHaveBeenCalledTimes(2));
  });

  it("cancels an in-progress scope edit without saving", async () => {
    vi.mocked(api.listMcpTokens).mockResolvedValue([
      { id: "t1", label: "Old", scope: "read", created_at: "x", last_used_at: null },
    ]);
    render(<McpTokensCard />);
    fireEvent.click(await screen.findByRole("button", { name: /Edit/i }));

    const select = screen.getByLabelText(/Scope for Old/i);
    fireEvent.change(select, { target: { value: "write" } });
    fireEvent.click(screen.getByRole("button", { name: /Cancel/i }));

    expect(api.updateMcpTokenScope).not.toHaveBeenCalled();
    expect(screen.queryByLabelText(/Scope for Old/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Save/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Cancel/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Edit/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Revoke/i })).toBeInTheDocument();
  });

  it("shows an error when updating scope fails", async () => {
    vi.mocked(api.listMcpTokens).mockResolvedValue([
      { id: "t1", label: "Old", scope: "read", created_at: "x", last_used_at: null },
    ]);
    vi.mocked(api.updateMcpTokenScope).mockRejectedValue(new Error("scope must be one of ..."));
    render(<McpTokensCard />);
    fireEvent.click(await screen.findByRole("button", { name: /Edit/i }));
    fireEvent.click(screen.getByRole("button", { name: /Save/i }));
    expect(await screen.findByText(/scope must be one of/i)).toBeInTheDocument();
  });
});
