// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * House time zone helpers (Issue #729).
 *
 * Every time Plenum shows or accepts is in the HOUSE's time zone (the add-on's
 * `timezone` option, reported by `GET /api/settings` as `timezone`), never the
 * browser's. Viewing Plenum from a phone in another zone must not shift a
 * vacation start, an Eco Suspend or a log range by the difference.
 *
 * This is the ONLY module allowed to turn instants into wall-clock text or
 * wall-clock input back into instants. An ESLint rule bans `toLocale*` and
 * `toISOString` elsewhere, the same way `<Frozen>` funnels `isCI` through one
 * place. Components reach these through `useTimeZone()` (contexts.ts), which
 * binds them to the active zone.
 *
 * Zones are IANA names (`America/New_York`) or, on the backend's OS-fallback
 * path, a fixed offset written `UTC±HH:MM` (`UTC` for zero). Intl cannot take
 * the latter as a `timeZone`, so fixed offsets are handled by shifting the
 * instant and formatting in UTC.
 *
 * The API's own input semantics are unchanged: callers send offset-bearing ISO
 * instants built by `fromZonedInput`, and the backend stores what it stored
 * before. See docs/time-zones.md.
 */

export type TimeKind = "datetime" | "date" | "time";

const FIXED_OFFSET = /^UTC(?:([+-])(\d{2}):(\d{2}))?$/;

/** Minutes east of UTC for a fixed-offset zone name, or null for an IANA zone. */
export function fixedOffsetMinutes(zone: string): number | null {
  const m = FIXED_OFFSET.exec(zone);
  if (!m) return null;
  if (!m[1]) return 0;
  const mins = Number(m[2]) * 60 + Number(m[3]);
  return m[1] === "-" ? -mins : mins;
}

/** The browser's own zone — used only as a labelled fallback. */
export function browserZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

/** True when Intl recognises the zone (fixed offsets count as valid). */
export function isValidZone(zone: string): boolean {
  if (fixedOffsetMinutes(zone) !== null) return true;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: zone });
    return true;
  } catch {
    return false;
  }
}

function toDate(value: string | number | Date): Date {
  return value instanceof Date ? value : new Date(value);
}

interface Parts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
}

/** Wall-clock components of an instant in a zone. */
function wallClock(zone: string, ms: number): Parts {
  const fixed = fixedOffsetMinutes(zone);
  if (fixed !== null) {
    const d = new Date(ms + fixed * 60_000);
    return {
      year: d.getUTCFullYear(),
      month: d.getUTCMonth() + 1,
      day: d.getUTCDate(),
      hour: d.getUTCHours(),
      minute: d.getUTCMinutes(),
      second: d.getUTCSeconds(),
    };
  }
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone: zone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  const get = (type: string) =>
    Number(fmt.formatToParts(new Date(ms)).find((p) => p.type === type)?.value);
  return {
    year: get("year"),
    month: get("month"),
    day: get("day"),
    hour: get("hour"),
    minute: get("minute"),
    second: get("second"),
  };
}

/** The zone's UTC offset in minutes at the given instant (DST-aware). */
export function offsetMinutesAt(zone: string, ms: number): number {
  const fixed = fixedOffsetMinutes(zone);
  if (fixed !== null) return fixed;
  const p = wallClock(zone, ms);
  const asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second);
  return Math.round((asUtc - Math.floor(ms / 1000) * 1000) / 60_000);
}

function formatOffset(minutes: number): string {
  if (minutes === 0) return "UTC";
  const sign = minutes > 0 ? "+" : "-";
  const abs = Math.abs(minutes);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `UTC${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`;
}

/**
 * The zone's short name at an instant: `EST` in winter, `EDT` in summer. Zones
 * Intl has no letter abbreviation for (it answers `GMT+5:30`) are shown as
 * their offset, `UTC+05:30`, the closest honest shorthand.
 */
export function zoneShortName(zone: string, at: string | number | Date = Date.now()): string {
  // An unparseable `at` (a raw stored string the UI shows verbatim) falls back
  // to the zone's name right now rather than throwing.
  const parsed = toDate(at).getTime();
  const ms = Number.isNaN(parsed) ? Date.now() : parsed;
  if (fixedOffsetMinutes(zone) !== null) return formatOffset(offsetMinutesAt(zone, ms));
  const name =
    new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: "short" })
      .formatToParts(new Date(ms))
      .find((p) => p.type === "timeZoneName")?.value ?? "";
  if (/^[A-Z]{2,5}$/.test(name)) return name;
  return formatOffset(offsetMinutesAt(zone, ms));
}

/**
 * Format an instant in a zone, in the viewer's locale, matching exactly what
 * `toLocaleString` / `toLocaleDateString` / `toLocaleTimeString` produced before
 * #729 — only the zone changes. `options` override the defaults.
 */
export function formatInZone(
  zone: string,
  value: string | number | Date,
  kind: TimeKind = "datetime",
  options: Intl.DateTimeFormatOptions = {}
): string {
  const d = toDate(value);
  if (Number.isNaN(d.getTime())) return typeof value === "string" ? value : "";
  const fixed = fixedOffsetMinutes(zone);
  const shifted = fixed === null ? d : new Date(d.getTime() + fixed * 60_000);
  const opts = { ...options, timeZone: fixed === null ? zone : "UTC" };
  if (kind === "date") return shifted.toLocaleDateString(undefined, opts);
  if (kind === "time") return shifted.toLocaleTimeString(undefined, opts);
  return shifted.toLocaleString(undefined, opts);
}

const pad2 = (n: number) => String(n).padStart(2, "0");

/** An instant as a `datetime-local` value (`YYYY-MM-DDTHH:mm`) in a zone. */
export function toZonedInput(zone: string, value: string | number | Date): string {
  const p = wallClock(zone, toDate(value).getTime());
  return `${p.year}-${pad2(p.month)}-${pad2(p.day)}T${pad2(p.hour)}:${pad2(p.minute)}`;
}

/** Today's calendar date (`YYYY-MM-DD`) in a zone. */
export function todayInZone(zone: string, now: number = Date.now()): string {
  const p = wallClock(zone, now);
  return `${p.year}-${pad2(p.month)}-${pad2(p.day)}`;
}

/** The instant `ms` milliseconds before now, as ISO-8601 UTC (relative presets). */
export function instantAgo(ms: number, now: number = Date.now()): string {
  return new Date(now - ms).toISOString();
}

/** Shift a `YYYY-MM-DD` calendar date by whole days (pure date arithmetic). */
export function addDays(ymd: string, days: number): string {
  const [y, m, d] = ymd.split("-").map(Number);
  const t = new Date(Date.UTC(y, m - 1, d + days));
  return `${t.getUTCFullYear()}-${pad2(t.getUTCMonth() + 1)}-${pad2(t.getUTCDate())}`;
}

export interface ZonedInstant {
  /** The UTC instant, as ISO-8601 (`…Z`). */
  iso: string;
  /**
   * True when the typed wall-clock time does not exist in the zone (the hour
   * skipped by a spring-forward DST change) and was moved forward by the gap.
   */
  adjusted: boolean;
}

const INPUT = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/;

/**
 * A `datetime-local` value typed as wall-clock time IN THE ZONE → the instant
 * it names. Returns null for an empty or malformed value.
 *
 * DST rules (docs/time-zones.md):
 *  - overlap (fall-back hour, the time happens twice): the EARLIER instant;
 *  - gap (spring-forward hour, the time never happens): moved forward by the
 *    gap, e.g. 02:30 → 03:30, with `adjusted: true` so the form can say so.
 */
export function fromZonedInput(zone: string, value: string): ZonedInstant | null {
  const m = INPUT.exec(value);
  if (!m) return null;
  const guess = Date.UTC(
    Number(m[1]),
    Number(m[2]) - 1,
    Number(m[3]),
    Number(m[4]),
    Number(m[5]),
    Number(m[6] ?? 0)
  );
  // The two offsets that can apply near this wall-clock time: the zone's
  // offset a day either side brackets any single DST transition.
  const before = offsetMinutesAt(zone, guess - 86_400_000);
  const after = offsetMinutesAt(zone, guess + 86_400_000);
  const candidates = Array.from(new Set([before, after]))
    .map((off) => guess - off * 60_000)
    .filter((t) => offsetMinutesAt(zone, t) * 60_000 === guess - t)
    .sort((a, b) => a - b);
  if (candidates.length > 0) {
    return { iso: new Date(candidates[0]).toISOString(), adjusted: false };
  }
  // Gap: no instant shows this wall clock. Read it with the pre-transition
  // offset, which lands the same distance past the jump.
  const t = guess - Math.min(before, after) * 60_000;
  return { iso: new Date(t).toISOString(), adjusted: true };
}
