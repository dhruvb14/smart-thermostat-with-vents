import { describe, expect, it } from "vitest";
import { sanitizeRoomName } from "./roomNames";
import cases from "./roomNameCases.json";

describe("sanitizeRoomName", () => {
  // The shared vectors — backend/tests/test_mqtt_naming.py reads the same file,
  // so the two implementations of the rule cannot drift apart silently (#519).
  it.each(cases.cases)("sanitizes $raw to $sanitized", ({ raw, sanitized }) => {
    expect(sanitizeRoomName(raw)).toBe(sanitized);
  });

  it("is idempotent", () => {
    for (const { raw } of cases.cases) {
      const once = sanitizeRoomName(raw);
      expect(sanitizeRoomName(once)).toBe(once);
    }
  });

  it("collapses case variants onto the same key", () => {
    expect(sanitizeRoomName("Office")).toBe(sanitizeRoomName("office"));
    expect(sanitizeRoomName("Office")).toBe(sanitizeRoomName("OFFICE"));
  });

  it("keeps genuinely different names apart", () => {
    expect(sanitizeRoomName("Office")).not.toBe(sanitizeRoomName("Off Ice"));
    expect(sanitizeRoomName("Office")).not.toBe(sanitizeRoomName("off-ice"));
  });
});
