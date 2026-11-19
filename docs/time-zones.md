# Time zones

Plenum runs on one clock: the **house's** time zone, set by the add-on's
`timezone` option (or the `TIMEZONE` environment variable on standalone
Docker). Schedules fire in it, and since Issue #729 every time the UI shows or
accepts is in it too, whatever zone the device viewing Plenum is in.

So if you set a vacation return time from a phone while you are in Tokyo,
"Jan 15, 10:00 AM" means 10:00 AM at the house, not 10:00 AM in Tokyo, and the
banner shows it back as 10:00 AM.

## Where you see it

- **Settings page, Time zone card.** Shows the zone Plenum is using
  (`America/New_York`) and its current short name (`currently EST`). If the
  `timezone` option is missing or invalid, the card shows an orange warning
  naming the zone Plenum fell back to (the operating system's offset, written
  `UTC-05:00`, or `UTC`). If the zone could not be loaded at all, a red badge
  says the UI is showing your browser's zone instead.
- **Every time in the UI** carries the zone's short name as a tooltip: `EST`
  in winter, `EDT` in summer. Zones that have no letter abbreviation show
  their offset, such as `UTC+05:30`.
- **Every date/time picker** has an ⓘ marker after it. Hover it (or tap it on
  a touch screen) to see which zone you are typing in.

## Daylight-saving changes

A wall-clock time typed into a picker is turned into an exact instant with
these rules:

| Case | Example (New York) | What Plenum does |
|---|---|---|
| Normal | 10:00 AM on Jan 15 | That instant (15:00 UTC). |
| **Overlap**: the time happens twice (the fall-back hour) | 1:30 AM on the first Sunday of November | The **earlier** of the two (1:30 EDT). |
| **Gap**: the time never happens (the spring-forward hour) | 2:30 AM on the second Sunday of March | Moved **forward** by the gap (3:30 EDT), and the form says so under the picker before you save. |

## What did not change: the API

The conversion happens entirely in the browser. The REST API, MCP tools and
MQTT topics keep exactly the input rules they had before #729, because
automations and MCP clients already send these payloads:

| Field | Offset given (`…-05:00` or `Z`) | No offset ("naive") |
|---|---|---|
| Vacation `return_at` (`POST /api/settings/vacation-mode`) | That instant | **UTC** |
| Eco Suspend `resume_at` (`POST /api/thermostats/{id}/eco-suspend`) | That instant | **UTC** |
| Schedule `expires_at` | Stored as given | **House-local** wall clock |
| Logs `since` / `until` | Compared as text against stored UTC timestamps, so send the `Z` form (the UI does) | **UTC** |
| Metrics `start` / `end` | — (calendar dates) | **House-local** dates |
| MQTT `…/vacation_mode/return_at/set`, `…/eco_suspend_until/set` | That instant | **House-local** (see [mqtt](./mqtt.md)) |

The UI always sends an instant with an offset, so these rules never decide
what a UI-entered time means. If you write automations, include an offset and
the question never comes up.

`GET /api/settings` reports the zone as `timezone` (an IANA name, or
`UTC±HH:MM` on the fallback path) and `timezone_fallback` (`true` when the
option is missing or invalid).

## Upgrading

Nothing stored is rewritten. Values saved before #729 read back byte-identical,
and the integration test `test_timezone_contract.py` pins that. What changes is
only how the UI shows them: a browser that was not in the house's zone used to
show times in its own zone, and now shows the house's.

## For developers

- `smart_vent/frontend/src/time.ts` is the only module allowed to turn an
  instant into wall-clock text, or wall-clock input back into an instant. An
  ESLint rule bans `toLocaleString`, `toLocaleDateString`,
  `toLocaleTimeString` and `toISOString` everywhere else.
- Components use `useTimeZone()` from `contexts.ts`, `<Time value=…>` for an
  instant, `<HouseTime>` for text that is already house-local (a schedule's
  `HH:MM`, the backend's "next Wed 6:00 PM" label), `<ZoneHint />` after a
  picker and `<DstAdjustedHint value=…>` under a datetime picker.
- Vitest runs with `TZ=Asia/Tokyo`, so a test that accidentally uses the
  browser's zone fails instead of passing on a UTC machine.
- The E2E stack runs the house in `America/New_York` with the browser pinned
  to UTC; `e2e/tests/time-zones.spec.ts` views it from `Asia/Tokyo` and checks
  the stored instant, the shown wall clock and the tooltip.
