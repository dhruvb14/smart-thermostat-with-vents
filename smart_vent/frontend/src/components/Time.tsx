// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from "react";
import { useTimeZone } from "../contexts";
import type { TimeKind } from "../time";

interface TimeProps {
  /** An instant: ISO-8601 with offset or `Z`, epoch ms, or a Date. */
  value: string | number | Date;
  kind?: TimeKind;
  options?: Intl.DateTimeFormatOptions;
}

/**
 * An instant rendered in the house's time zone, with the zone's short name
 * (`EST`) as its tooltip (Issue #729). Use this for every time the UI shows;
 * the ESLint rule in eslint.config.js bans formatting dates any other way.
 */
export default function Time({ value, kind = "datetime", options }: TimeProps) {
  const tz = useTimeZone();
  const d = value instanceof Date ? value : new Date(value);
  const valid = !Number.isNaN(d.getTime());
  return (
    <time dateTime={valid ? d.toISOString() : undefined} title={valid ? tz.tooltip(d) : undefined}>
      {tz.format(value, kind, options)}
    </time>
  );
}

/**
 * The marker after a date/time input saying which zone it is typed in. Hover
 * shows the short name as a tooltip; tap (touch screens have no hover) toggles
 * it inline, so the zone is discoverable before typing, not only after.
 */
export function ZoneHint({ at }: { at?: string | number | Date }) {
  const tz = useTimeZone();
  const [open, setOpen] = useState(false);
  const label = tz.tooltip(at);
  return (
    <button
      type="button"
      className="zone-hint"
      title={label}
      aria-label={`Times are in ${label}`}
      aria-expanded={open}
      onClick={() => setOpen((v) => !v)}
    >
      ⓘ{open && <span className="zone-hint-text"> {label}</span>}
    </button>
  );
}

/**
 * Text that is ALREADY house-local wall-clock time — a schedule's `HH:MM`, a
 * schedule expiry, the backend's "next … Wed 6:00 PM" label — given the same
 * zone tooltip as `<Time>` so every time in the UI says which zone it is in.
 */
export function HouseTime({ children }: { children: React.ReactNode }) {
  const tz = useTimeZone();
  return <span title={tz.tooltip()}>{children}</span>;
}
