import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import Login from "./Login";
import * as api from "../api";

vi.mock("../api");

describe("Login — uncovered branches", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** The suite-wide setup stubs `location` with a bare object (no `search`);
   *  re-stub it so the component can read a query string. */
  const stubSearch = (search: string) =>
    vi.stubGlobal("location", { pathname: "/", protocol: "http:", host: "localhost", search });

  // ── Double-submit guard (line 42) ─────────────────────────────────────────

  it("ignores a second submit while the first is still in flight", async () => {
    // The button disables itself, but a second submit can still arrive from the
    // keyboard (Enter in a text field) before the first request settles. Only
    // one credential check must reach the backend.
    let resolveLogin!: (v: { ok: boolean }) => void;
    vi.mocked(api.login).mockReturnValue(
      new Promise<{ ok: boolean }>((res) => {
        resolveLogin = res;
      })
    );
    const onSuccess = vi.fn();
    const { container } = render(<Login onSuccess={onSuccess} />);
    const form = container.querySelector("form") as HTMLFormElement;

    fireEvent.change(screen.getByLabelText(/Username/i), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText(/Password/i), { target: { value: "pw" } });

    fireEvent.submit(form);
    expect(await screen.findByRole("button", { name: /Signing in…/i })).toBeInTheDocument();

    // Second submit lands while `submitting` is still true → dropped.
    fireEvent.submit(form);
    expect(api.login).toHaveBeenCalledTimes(1);

    resolveLogin({ ok: true });
    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
  });

  // ── Non-Error rejection (line 49) ─────────────────────────────────────────

  it("shows a generic message when the login call rejects with a non-Error", async () => {
    // A transport-level failure can reject with a bare value; the alert must
    // still carry readable copy rather than a stringified object.
    vi.mocked(api.login).mockRejectedValue({ status: 502 });
    const onSuccess = vi.fn();
    render(<Login onSuccess={onSuccess} />);

    fireEvent.change(screen.getByLabelText(/Username/i), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText(/Password/i), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: /Sign in/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Login failed");
    expect(onSuccess).not.toHaveBeenCalled();
    // The form comes back out of the submitting state so the user can retry.
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });

  // ── OIDC callback error surfaced on the SSO screen (line 67) ──────────────

  it("surfaces an OIDC round-trip failure on the SSO screen (#464)", () => {
    // The backend redirects back to the UI with ?login_error=<reason> when the
    // IdP round-trip fails; the OIDC screen (not just the password form) must
    // render that reason.
    stubSearch("?login_error=sso_forbidden");

    render(<Login onSuccess={vi.fn()} oidcEnabled oidcProviderName="Authelia" />);

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Your account is not permitted to access Plenum.");
    expect(alert.className).toContain("login-error");
    // The SSO button is still offered so the user can retry.
    expect(screen.getByRole("link", { name: /Sign in with Authelia/i })).toBeInTheDocument();
  });

  it("shows no alert on the SSO screen when the URL carries no error tag", () => {
    stubSearch("");
    render(<Login onSuccess={vi.fn()} oidcEnabled />);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
