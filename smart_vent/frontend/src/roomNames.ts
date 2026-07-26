/**
 * Room-name sanitisation, mirroring `backend/mqtt/naming.py` (Issue #519).
 *
 * MQTT addresses a room by its sanitised name as well as by its GUID, and
 * sanitising is lossy — "Office", "office", and "OFFICE" all collapse to
 * `office` — so room names must be unique on the *sanitised* form. The backend
 * is the enforcing copy; this one exists purely so the Rooms form can say so
 * while the user is still typing instead of only on an API rejection.
 *
 * The two implementations must agree exactly. `roomNameCases.json` holds the
 * shared vectors and both sides' tests read it, so a change to either copy
 * without the other fails CI.
 */

/** Lower-case, keep `[a-z0-9_-]`, collapse anything else to `_`, trim separators. */
export function sanitizeRoomName(raw: string): string {
  if (!raw) return "";
  return raw
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "_")
    .replace(/^[_-]+|[_-]+$/g, "");
}
