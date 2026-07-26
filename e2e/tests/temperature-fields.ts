/**
 * Single source of truth for every temperature field that any Plenum
 * write endpoint accepts.
 *
 * Two enforcement mechanisms keep this list honest:
 *
 *  1. `temperature-units.spec.ts` exercises a round-trip in both °F
 *     and °C for each entry with `ui: true`. Each test tags itself
 *     with `// @covers: <field>[, <field>...]` so the parity test
 *     below can verify coverage by grep.
 *
 *  2. `smart_vent/backend/tests/test_temperature_field_parity.py`
 *     compares this manifest to `TEMPERATURE_FIELDS` in
 *     `smart_vent/backend/api/routes.py` (both the field set and
 *     the kind for each), and asserts every `ui: true` entry has at
 *     least one `@covers:` mention in the e2e spec.
 *
 * Adding a new temperature field anywhere on a write boundary?
 * - Add the entry here AND in `routes.py`'s `TEMPERATURE_FIELDS` dict.
 * - If a UI page writes it, set `ui: true` and add a `@covers:` line
 *   to the matching test in `temperature-units.spec.ts`.
 *
 * The parity test fails CI loudly if any of these go out of sync.
 */

/** Conversion kind — drives `_to_f` vs `_delta_to_f` on the backend
 * and whether `null` is accepted as a value. */
export type TempKind =
  | "absolute"
  | "absolute_nullable"
  | "delta"
  | "delta_nullable";

export interface TempField {
  /** Body key exactly as it appears on the wire. */
  field: string;
  /** Conversion kind — must match `routes.py` `TEMPERATURE_FIELDS`. */
  kind: TempKind;
  /** Has at least one UI write path. If true, the e2e spec must
   * cover this field via a `// @covers:` marker. API-only fields
   * (e.g. `target_temp` on `/override`) set `ui: false`. */
  ui: boolean;
  /** Human-readable endpoints the field reaches. Documentation only —
   * not used in parity enforcement. */
  endpoints: string[];
}

export const TEMPERATURE_FIELDS: TempField[] = [
  // ── Thermostat config — POST /api/thermostats, PUT /api/thermostats/{id}
  {
    field: "default_temp",
    kind: "absolute_nullable",
    ui: true,
    endpoints: ["POST /api/thermostats", "PUT /api/thermostats/{id}"],
  },
  {
    field: "min_setpoint",
    kind: "absolute",
    ui: true,
    endpoints: ["POST /api/thermostats", "PUT /api/thermostats/{id}"],
  },
  {
    field: "max_setpoint",
    kind: "absolute",
    ui: true,
    endpoints: ["POST /api/thermostats", "PUT /api/thermostats/{id}"],
  },
  {
    field: "deadband",
    kind: "delta",
    ui: true,
    endpoints: ["POST /api/thermostats", "PUT /api/thermostats/{id}"],
  },
  {
    field: "overshoot_delta",
    kind: "delta",
    ui: true,
    endpoints: ["POST /api/thermostats", "PUT /api/thermostats/{id}"],
  },
  {
    field: "cooling_lockout_below_f",
    kind: "absolute_nullable",
    ui: true,
    endpoints: ["POST /api/thermostats", "PUT /api/thermostats/{id}"],
  },

  // ── Room — POST /api/rooms, PUT /api/rooms/{id}
  {
    field: "system_wide_temp",
    kind: "absolute_nullable",
    ui: true,
    endpoints: ["POST /api/rooms", "PUT /api/rooms/{id}"],
  },
  {
    field: "temp_offset",
    kind: "delta",
    ui: true,
    endpoints: ["POST /api/rooms", "PUT /api/rooms/{id}"],
  },
  // ── Deadband override. The same field name on two write boundaries:
  // per-room (Issue #277, written by the Rooms modal) and per-schedule
  // (Issue #517, written by the schedule modal's Temperature drift radio).
  // Nullable delta; null clears the override so the next level down inherits
  // (schedule → room → thermostat).
  {
    field: "deadband_override",
    kind: "delta_nullable",
    ui: true,
    endpoints: [
      "POST /api/rooms",
      "PUT /api/rooms/{id}",
      "POST /api/rooms/{id}/schedules",
      "PUT /api/rooms/{id}/schedules/{sid}",
    ],
  },
  // ── Ambient-aware presence suppression / pre-cool (Issue #248).
  // Delta fields written by the Rooms modal; covered by the round-trip in
  // temperature-units.spec.ts (// @covers).
  {
    field: "ambient_suppression_min_differential",
    kind: "delta",
    ui: true,
    endpoints: ["POST /api/rooms", "PUT /api/rooms/{id}"],
  },
  {
    field: "ambient_suppression_deadband",
    kind: "delta",
    ui: true,
    endpoints: ["POST /api/rooms", "PUT /api/rooms/{id}"],
  },

  // ── Eco Mode (Issue #404). The same field name is written on both the
  // thermostat (non-null) and room (nullable, null = inherit) write paths, so
  // each is registered once with the nullable kind (the conversion is identical
  // — _to_f for absolutes, _delta_to_f for deltas). eco_mode_enabled is a bool,
  // not a temperature, so it is not in this manifest.
  {
    field: "eco_cooling_outdoor_threshold",
    kind: "absolute_nullable",
    ui: true,
    endpoints: [
      "POST /api/thermostats",
      "PUT /api/thermostats/{id}",
      "POST /api/rooms",
      "PUT /api/rooms/{id}",
    ],
  },
  {
    field: "eco_cooling_full_drift_temp",
    kind: "absolute_nullable",
    ui: true,
    endpoints: [
      "POST /api/thermostats",
      "PUT /api/thermostats/{id}",
      "POST /api/rooms",
      "PUT /api/rooms/{id}",
    ],
  },
  {
    field: "eco_cooling_max_drift",
    kind: "delta_nullable",
    ui: true,
    endpoints: [
      "POST /api/thermostats",
      "PUT /api/thermostats/{id}",
      "POST /api/rooms",
      "PUT /api/rooms/{id}",
    ],
  },
  {
    field: "eco_heating_outdoor_threshold",
    kind: "absolute_nullable",
    ui: true,
    endpoints: [
      "POST /api/thermostats",
      "PUT /api/thermostats/{id}",
      "POST /api/rooms",
      "PUT /api/rooms/{id}",
    ],
  },
  {
    field: "eco_heating_full_drift_temp",
    kind: "absolute_nullable",
    ui: true,
    endpoints: [
      "POST /api/thermostats",
      "PUT /api/thermostats/{id}",
      "POST /api/rooms",
      "PUT /api/rooms/{id}",
    ],
  },
  {
    field: "eco_heating_max_drift",
    kind: "delta_nullable",
    ui: true,
    endpoints: [
      "POST /api/thermostats",
      "PUT /api/thermostats/{id}",
      "POST /api/rooms",
      "PUT /api/rooms/{id}",
    ],
  },
  {
    field: "eco_hysteresis_band",
    kind: "delta_nullable",
    ui: true,
    endpoints: [
      "POST /api/thermostats",
      "PUT /api/thermostats/{id}",
      "POST /api/rooms",
      "PUT /api/rooms/{id}",
    ],
  },

  // ── Schedules — POST/PUT /api/rooms/{id}/schedules[/{sid}]
  // Also accepted by POST /api/rooms/{id}/override (API-only path, no UI),
  // which is why the field is `ui: true` despite some callers being headless.
  {
    field: "target_temp",
    kind: "absolute",
    ui: true,
    endpoints: [
      "POST /api/rooms/{id}/schedules",
      "PUT /api/rooms/{id}/schedules/{sid}",
      "POST /api/rooms/{id}/override (API-only)",
    ],
  },
];
