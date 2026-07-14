import React, { useState } from "react";
import { login as apiLogin } from "../api";
import { loginErrorMessage } from "./loginError";

/**
 * Direct-port login screen (#373, #464). Shown by the App-level auth gate only
 * when `require_auth` is on AND the caller is not already authenticated (i.e.
 * reached a published port directly, not through the Home Assistant sidebar).
 *
 * Two modes, driven by the `/api/auth/status` probe:
 *  - **OIDC single sign-on** (`oidcEnabled`, #464): renders a "Sign in with
 *    {provider}" button that navigates to the backend `/api/auth/oidc/login`
 *    endpoint, which redirects to the identity provider. The HA username/password
 *    form is not shown (and is disabled server-side in this mode).
 *  - **HA account** (default, #373): username + password validated against the
 *    Supervisor `/auth` backend via `/api/auth/login`; on success the backend
 *    sets an HttpOnly session cookie and the gate re-checks and renders the app.
 */

function initialLoginError(): string | null {
  return typeof window === "undefined" ? null : loginErrorMessage(window.location.search);
}

export default function Login({
  onSuccess,
  oidcEnabled = false,
  oidcProviderName = "SSO",
  oidcLoginUrl = "/api/auth/oidc/login",
}: {
  onSuccess: () => void;
  oidcEnabled?: boolean;
  oidcProviderName?: string;
  oidcLoginUrl?: string;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(initialLoginError);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await apiLogin(username, password);
      onSuccess();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setSubmitting(false);
    }
  };

  if (oidcEnabled) {
    return (
      <div className="login-screen">
        <div className="card login-card">
          <div className="login-brand">
            <span className="nav-icon">🌡</span> Plenum
          </div>
          <div className="card-title">Sign in</div>
          <p className="form-hint login-intro">
            Sign in with your organization account. Opening Plenum from the Home Assistant sidebar
            signs you in automatically — this is only for accessing a directly-published port.
          </p>
          {error && (
            <div className="badge badge-red login-error" role="alert">
              {error}
            </div>
          )}
          {/* A real navigation (not a fetch): the backend 302s to the IdP. Using
              an anchor keeps it accessible (right-clickable) and testable. */}
          <a className="btn btn-primary login-submit" href={oidcLoginUrl}>
            Sign in with {oidcProviderName}
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="login-screen">
      <form className="card login-card" onSubmit={submit}>
        <div className="login-brand">
          <span className="nav-icon">🌡</span> Plenum
        </div>
        <div className="card-title">Sign in</div>
        <p className="form-hint login-intro">
          Use your Home Assistant username and password. Opening Plenum from the Home Assistant
          sidebar signs you in automatically — this login is only for accessing a directly-published
          port.
        </p>
        <div className="form-group">
          <label className="form-label" htmlFor="login-username">
            Username
          </label>
          <input
            id="login-username"
            className="form-control"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </div>
        <div className="form-group">
          <label className="form-label" htmlFor="login-password">
            Password
          </label>
          <input
            id="login-password"
            className="form-control"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        {error && (
          <div className="badge badge-red login-error" role="alert">
            {error}
          </div>
        )}
        <button
          className="btn btn-primary login-submit"
          type="submit"
          disabled={submitting || !username || !password}
        >
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
