import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import Login from "./Login";
import { loginErrorMessage } from "./loginError";
import * as api from "../api";

vi.mock("../api");

describe("Login", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("submits credentials and calls onSuccess", async () => {
    vi.mocked(api.login).mockResolvedValue({ ok: true });
    const onSuccess = vi.fn();
    render(<Login onSuccess={onSuccess} />);

    fireEvent.change(screen.getByLabelText(/Username/i), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText(/Password/i), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: /Sign in/i }));

    await waitFor(() => expect(api.login).toHaveBeenCalledWith("alice", "pw"));
    await waitFor(() => expect(onSuccess).toHaveBeenCalled());
  });

  it("shows the error message on a failed login and does not call onSuccess", async () => {
    vi.mocked(api.login).mockRejectedValue(new Error("Invalid username or password"));
    const onSuccess = vi.fn();
    render(<Login onSuccess={onSuccess} />);

    fireEvent.change(screen.getByLabelText(/Username/i), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText(/Password/i), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: /Sign in/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Invalid username or password/i);
    expect(onSuccess).not.toHaveBeenCalled();
  });

  it("disables the submit button until both fields are filled", () => {
    render(<Login onSuccess={vi.fn()} />);
    const btn = screen.getByRole("button", { name: /Sign in/i });
    expect(btn).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Username/i), { target: { value: "a" } });
    expect(btn).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Password/i), { target: { value: "b" } });
    expect(btn).not.toBeDisabled();
  });

  describe("OIDC mode (#464)", () => {
    it("renders a 'Sign in with <provider>' link to the login endpoint, no password form", () => {
      render(
        <Login
          onSuccess={vi.fn()}
          oidcEnabled
          oidcProviderName="Authelia"
          oidcLoginUrl="/api/auth/oidc/login"
        />
      );
      const link = screen.getByRole("link", { name: /Sign in with Authelia/i });
      expect(link).toHaveAttribute("href", "/api/auth/oidc/login");
      // The HA username/password form is not rendered in OIDC mode.
      expect(screen.queryByLabelText(/Username/i)).toBeNull();
      expect(screen.queryByLabelText(/Password/i)).toBeNull();
      // OIDC login is a full navigation, so api.login must not be involved.
      expect(api.login).not.toHaveBeenCalled();
    });

    it("maps login_error reasons to messages (pure helper)", () => {
      expect(loginErrorMessage("?login_error=sso_forbidden")).toMatch(/not permitted/i);
      expect(loginErrorMessage("?login_error=sso_state")).toMatch(/expired/i);
      expect(loginErrorMessage("?login_error=sso_cancelled")).toMatch(/cancelled/i);
      expect(loginErrorMessage("?login_error=sso_failed")).toMatch(/failed/i);
      // Unknown reason → generic; no tag → null.
      expect(loginErrorMessage("?login_error=weird")).toMatch(/Sign-in failed/i);
      expect(loginErrorMessage("")).toBeNull();
    });
  });
});
