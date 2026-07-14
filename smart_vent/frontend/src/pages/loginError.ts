// Friendly copy for the `?login_error=<reason>` tag the OIDC callback (#464) adds
// to the redirect URL when a single-sign-on round-trip fails. Reasons are fixed
// backend constants (backend/api/routes.py). Kept in its own module so Login.tsx
// stays a component-only file (React Fast Refresh / react-refresh lint rule).
const LOGIN_ERROR_MESSAGES: Record<string, string> = {
  sso_cancelled: "Sign-in was cancelled at the identity provider.",
  sso_state: "Your sign-in session expired. Please try again.",
  sso_forbidden: "Your account is not permitted to access Plenum.",
  sso_failed: "Single sign-on failed. Please try again.",
};

/** Map a URL query string's `login_error` tag to a user message. Unknown reasons
 * collapse to a generic message; no tag → null. */
export function loginErrorMessage(search: string): string | null {
  const reason = new URLSearchParams(search).get("login_error");
  if (!reason) return null;
  return LOGIN_ERROR_MESSAGES[reason] ?? "Sign-in failed. Please try again.";
}
