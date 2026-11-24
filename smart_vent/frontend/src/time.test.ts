// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, it, expect, vi, afterEach } from "vitest";
import {
  addDays,
  browserZone,
  fixedOffsetMinutes,
  formatInZone,
  fromZonedInput,
  instantAgo,
  isValidZone,
  offsetMinutesAt,
  todayInZone,
  toZonedInput,
  zoneShortName,
} from "./time";

// The vitest process runs with TZ=Asia/Tokyo (vite.config.ts) so nothing here
// can pass by the browser zone happening to equal the house zone.
const NY = "America/New_York";
const WINTER = Date.UTC(2026, 0, 15, 17, 30); // 12:30 EST
const SUMMER = Date.UTC(2026, 6, 15, 16, 30); // 12:30 EDT

describe("fixedOffsetMinutes", () => {
  it.each([
    ["UTC", 0],
    ["UTC+05:30", 330],
    ["UTC-09:30", -570],
    [NY, null],
    ["UTC+5", null],
  ])("%s → %s", (zone, expected) => {
    expect(fixedOffsetMinutes(zone)).toBe(expected);
  });
});

describe("browserZone / isValidZone", () => {
  afterEach(() => vi.restoreAllMocks());

  it("reports the runner's zone", () => {
    expect(browserZone()).toBe("Asia/Tokyo");
  });

  it("falls back to UTC when Intl reports no zone", () => {
    vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({
      timeZone: "",
    } as Intl.ResolvedDateTimeFormatOptions);
    expect(browserZone()).toBe("UTC");
  });

  it("accepts IANA names and fixed offsets, rejects junk", () => {
    expect(isValidZone(NY)).toBe(true);
    expect(isValidZone("UTC-05:00")).toBe(true);
    expect(isValidZone("Not/AZone")).toBe(false);
  });
});

describe("offsetMinutesAt", () => {
  it("is DST-aware for IANA zones", () => {
    expect(offsetMinutesAt(NY, WINTER)).toBe(-300);
    expect(offsetMinutesAt(NY, SUMMER)).toBe(-240);
  });
  it("is constant for fixed zones", () => {
    expect(offsetMinutesAt("UTC+05:30", WINTER)).toBe(330);
  });
});

describe("zoneShortName", () => {
  it("gives the abbreviation in force at the instant", () => {
    expect(zoneShortName(NY, WINTER)).toBe("EST");
    expect(zoneShortName(NY, new Date(SUMMER))).toBe("EDT");
    expect(zoneShortName("UTC", WINTER)).toBe("UTC");
  });
  it("uses the UTC offset where Intl has no letter abbreviation", () => {
    expect(zoneShortName("Asia/Kolkata", WINTER)).toBe("UTC+05:30");
    expect(zoneShortName("America/St_Johns", WINTER)).toMatch(/^(NST|UTC-03:30)$/);
  });
  it("formats fixed-offset zones as offsets", () => {
    expect(zoneShortName("UTC-09:30", WINTER)).toBe("UTC-09:30");
    expect(zoneShortName("UTC", WINTER)).toBe("UTC");
  });
  it("falls back to now for an unparseable instant instead of throwing", () => {
    expect(zoneShortName(NY, "sometime-next-week")).toMatch(/^E[SD]T$/);
  });
  it("defaults to now", () => {
    expect(zoneShortName(NY)).toMatch(/^E[SD]T$/);
  });
});

describe("formatInZone", () => {
  it("formats in the house zone, not the browser's", () => {
    expect(
      formatInZone(NY, WINTER, "time", { hour: "2-digit", minute: "2-digit", hour12: false })
    ).toBe("12:30");
    expect(
      formatInZone(NY, new Date(WINTER).toISOString(), "date", { timeZoneName: undefined })
    ).toBe(new Date(WINTER).toLocaleDateString(undefined, { timeZone: NY }));
    expect(formatInZone(NY, WINTER)).toBe(
      new Date(WINTER).toLocaleString(undefined, { timeZone: NY })
    );
  });
  it("handles fixed-offset zones", () => {
    expect(
      formatInZone("UTC+05:30", WINTER, "time", {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    ).toBe("23:00");
  });
  it("returns an unparseable string verbatim, and empty for other junk", () => {
    expect(formatInZone(NY, "sometime-next-week")).toBe("sometime-next-week");
    expect(formatInZone(NY, Number.NaN)).toBe("");
  });
});

describe("toZonedInput / todayInZone / addDays / instantAgo", () => {
  it("renders the wall clock in the zone", () => {
    expect(toZonedInput(NY, WINTER)).toBe("2026-01-15T12:30");
    expect(toZonedInput("UTC-09:30", WINTER)).toBe("2026-01-15T08:00");
  });
  it("gives the house's date across a midnight the browser disagrees with", () => {
    // 03:30Z on 16 Jan: already the 16th in Tokyo (the browser), still the
    // 15th in New York (the house).
    const t = Date.UTC(2026, 0, 16, 3, 30);
    expect(todayInZone(NY, t)).toBe("2026-01-15");
    expect(todayInZone("Asia/Tokyo", t)).toBe("2026-01-16");
    expect(todayInZone(NY)).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });
  it("adds and subtracts calendar days across month/year ends", () => {
    expect(addDays("2026-03-01", -1)).toBe("2026-02-28");
    expect(addDays("2025-12-31", 1)).toBe("2026-01-01");
  });
  it("computes relative instants", () => {
    expect(instantAgo(3_600_000, Date.UTC(2026, 0, 1, 1))).toBe("2026-01-01T00:00:00.000Z");
    expect(instantAgo(0)).toMatch(/Z$/);
  });
});

describe("fromZonedInput", () => {
  it("reads typed wall-clock time in the house zone", () => {
    expect(fromZonedInput(NY, "2026-01-15T12:30")).toEqual({
      iso: "2026-01-15T17:30:00.000Z",
      adjusted: false,
    });
    expect(fromZonedInput(NY, "2026-07-15T12:30:15")?.iso).toBe("2026-07-15T16:30:15.000Z");
    expect(fromZonedInput("UTC+05:30", "2026-01-15T23:00")?.iso).toBe("2026-01-15T17:30:00.000Z");
  });
  it("returns null for empty or malformed input", () => {
    expect(fromZonedInput(NY, "")).toBeNull();
    expect(fromZonedInput(NY, "15/01/2026 12:30")).toBeNull();
  });
  it("picks the EARLIER instant in a fall-back overlap", () => {
    // 01:30 on 1 Nov 2026 happens twice in New York: EDT first.
    expect(fromZonedInput(NY, "2026-11-01T01:30")).toEqual({
      iso: "2026-11-01T05:30:00.000Z",
      adjusted: false,
    });
  });
  it("moves a time skipped by spring-forward on by the gap, and says so", () => {
    // 02:30 on 8 Mar 2026 never happens in New York; it is saved as 03:30 EDT.
    expect(fromZonedInput(NY, "2026-03-08T02:30")).toEqual({
      iso: "2026-03-08T07:30:00.000Z",
      adjusted: true,
    });
  });
  it("round-trips every hour of a year, through both DST changes", () => {
    const start = Date.UTC(2026, 0, 1);
    // Every 7th hour covers every hour-of-day across the year (7 and 24 are
    // coprime) in ~1,250 samples instead of 8,760, which keeps this inside the
    // test timeout under coverage instrumentation.
    for (let h = 0; h < 366 * 24; h += 7) {
      const t = start + h * 3_600_000;
      const back = fromZonedInput(NY, toZonedInput(NY, t));
      // The second 01:xx of fall-back maps to the first: same wall clock,
      // documented choice. Everything else must round-trip exactly.
      const repeatedHour = back!.iso !== new Date(t).toISOString();
      if (repeatedHour) {
        expect(Date.parse(back!.iso)).toBe(t - 3_600_000);
      } else {
        expect(back!.adjusted).toBe(false);
      }
    }
  });
});
