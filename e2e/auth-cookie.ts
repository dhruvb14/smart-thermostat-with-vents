// Session-cookie helper for the authentication visual-regression leg (#373).
//
// The auth E2E stack (docker-compose.test.auth.yml) runs Plenum with
// require_auth=true and no Home Assistant Supervisor, so every request is
// "direct" and the API requires a credential. There is no live Supervisor to
// log in against, so the harness authenticates by *minting a session cookie*
// signed with the same key the stack is pinned to (PLENUM_SESSION_SECRET). This
// mirrors backend/session.py::issue_token exactly — verified to round-trip
// against the Python verifier.
//
// The secret below is a TEST-ONLY fixed value; it is used only by this E2E
// stack and never appears in a real deployment.

import { createHmac } from "crypto";

export const E2E_SESSION_SECRET = "gjncHw3UQkBNvHhgRDhN5hQRcNpmvK48UPmZz-IV1PA";
export const SESSION_COOKIE = "plenum_session";

/**
 * Mint a Plenum session token, byte-for-byte compatible with
 * backend/session.py::issue_token:
 *   token = base64url(payload) + "." + base64url(HMAC_SHA256(secret, payload))
 *   payload = {"exp","iat","sub"}  (keys sorted, compact separators)
 * exp/iat use real time (session verification checks the wall clock, not the
 * pinned test clock) — the token itself is never rendered, so this doesn't
 * affect golden determinism.
 */
export function mintSession(user = "e2e-admin", ttlSeconds = 7 * 24 * 3600): string {
  const secret = Buffer.from(E2E_SESSION_SECRET, "base64url");
  const now = Math.floor(Date.now() / 1000);
  const payload = JSON.stringify({ exp: now + ttlSeconds, iat: now, sub: user });
  const raw = Buffer.from(payload, "utf-8").toString("base64url");
  const sig = createHmac("sha256", secret).update(raw).digest("base64url");
  return `${raw}.${sig}`;
}

/** True when the current E2E run targets the auth (require_auth=true) stack. */
export const AUTH_MODE = process.env.PLENUM_E2E_AUTH === "1";
