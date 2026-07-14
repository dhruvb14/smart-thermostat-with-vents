import React, { useState } from "react";
import { login as apiLogin } from "../api";

/**
 * Direct-port login screen (#373). Shown by the App-level auth gate only when
 * `require_auth` is on AND the caller is not already authenticated (i.e. reached
 * a published port directly, not through the Home Assistant sidebar). Validates
 * against the user's Home Assistant account via the backend `/api/auth/login`
 * → Supervisor `/auth` flow; on success the backend sets an HttpOnly session
 * cookie and the gate re-checks and renders the app.
 */
export default function Login({ onSuccess }: { onSuccess: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
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
