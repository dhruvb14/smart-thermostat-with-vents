# MQTT interface for Home Assistant automations

Plenum can expose its controls over MQTT so **Home Assistant automations drive
it directly** — enable a schedule when you leave, hold a room while a meeting is
on the calendar, clear presence when the house arms, flip vacation mode from a
travel calendar.

The REST API and the [MCP server](./mcp.md) can already do all of this, but
neither suits an automation: REST needs a hand-written `rest_command:` in
`configuration.yaml`, and MCP is not something the automation editor speaks.
MQTT is — and with discovery on, every control shows up as a **native Home
Assistant entity** you can pick from a dropdown, with no YAML at all.

> **Off by default, one switch.** The bridge never connects until the **MQTT
> bridge** toggle on Plenum's **Settings** page is turned on — the same shape as
> the MCP server's toggle. There is nothing to enable in the add-on
> configuration: with a broker available (HAOS's built-in Mosquitto, or
> `MQTT_HOST` on Docker), the toggle is ready on first boot.

---

## Setup

### Home Assistant OS / Supervised

1. Install the **Mosquitto broker** add-on if you have not already (restart
   Plenum if it was already running when you installed it).
2. Open Plenum → **Settings** → turn the **MQTT bridge** on.

That is all. Plenum asks the Supervisor for the built-in broker's address and
credentials at startup, so there is nothing to type and nothing to set in the
add-on configuration. The topic prefix defaults to the add-on slug — also
fetched from the Supervisor — which is why stable and beta can share one broker
without colliding. An add-on installed from a repository gets a slug (and so a
prefix) like `88b5ffac_plenum` or `88b5ffac_plenum_beta`; the hash stays in the
topics but is hidden from anything human-facing.

### Standalone Docker

There is no Supervisor, so name the broker yourself — `MQTT_HOST` is what makes
the bridge available:

```yaml
environment:
  MQTT_HOST: "mosquitto"
  MQTT_PORT: "1883"
  MQTT_USER: "plenum"
  MQTT_PASSWORD: "…"
  # No add-on slug exists without a Supervisor, so the prefix falls back to
  # "plenum". Set this if two Plenum containers share one broker.
  MQTT_TOPIC_PREFIX: "plenum"
```

Then turn the bridge on from **Settings**.

### Checking it worked

The **MQTT bridge** card on the Settings page shows the resolved broker, the
resolved topic prefix, whether discovery is on, and the live connection state.
The topic prefix is worth a look — it is derived from the add-on slug, so this
card is the only place it is visible.

---

## What you get

With discovery on (the default), Plenum publishes Home Assistant MQTT Discovery
configs and the controls appear as entities grouped into devices:

| Device | Contains |
|---|---|
| **One per room** | Clear presence, hold temperature, each of that room's schedules, and every room setting (offset, holdover, pre-cool/pre-heat, per-room Eco overrides) |
| **One per thermostat** | Setpoint bounds, deadband, overshoot, vacation HVAC mode, Eco Mode and its base values, Eco Suspend |
| **The app** (`Plenum App`) | Vacation mode + return-at, system on/off — also the hub device every room and thermostat shows as "Connected via" |

The instance's identity — the topic prefix, prettified (`plenum` → "Plenum",
`plenum_beta` → "Plenum Beta", with a HAOS repository hash like `88b5ffac_` or
a `local_` slug prefix dropped from the display form) — leads every device
name and fills the `manufacturer` field. The stable add-on publishes
"Plenum *Room*, by Plenum" connected via **Plenum App**; the beta publishes
"Plenum Beta *Room*, by Plenum Beta" connected via **Plenum Beta App**. Home
Assistant derives entity ids from device names, so beta entities register as
`*.plenum_beta_…` and stable's as `*.plenum_…` — two installs on one broker
stay apart at a glance, and in automations.

> **Entity ids are minted once.** HA suggests an entity id at first discovery
> and never renames the entity afterwards, so entities that existed before an
> upgrade that changed device naming keep their old ids (the device name
> updates in place). To re-mint them under the current names: delete the
> Plenum devices from HA's MQTT integration, then restart Plenum (or toggle
> the bridge off and on) — everything re-discovers within seconds. Automations
> that referenced the old ids need re-pointing afterwards.

Schedules are discovered **dynamically**: create one in Plenum and its switch
appears in Home Assistant; delete it and the entity is removed. A schedule with
a [name](./schedules.md) uses it; an unnamed one falls back to its id.

> **Home Assistant 2026.5 or newer** is needed for the **Vacation Return At**
> and **Eco Suspend Until** entities — they use HA's MQTT `datetime` platform,
> which older versions silently ignore. Everything else appears on any
> discovery-capable HA, and the raw `.../return_at/set` /
> `.../eco_suspend_until/set` topics work regardless of version.

### What is deliberately *not* exposed

The equipment-protection settings — short-cycle protection
(`min_cycle_runtime_min`, `min_cycle_offtime_min`), `cycle_timeout_hours`, the
airflow floor (`min_open_vents_fraction`, `max_vent_closed_min`),
`unavailable_abort_after_min`, `overflow_during_min_runtime`, and
`reconciliation_interval_min` — are **not** on MQTT and are reachable only from
the Settings and Thermostats pages.

This is deliberate. MQTT's trust boundary is the broker's ACLs, which is a
weaker gate than the [authentication](./auth.md) in front of the web UI and MCP.
These settings exist to stop real equipment damage, so they stay behind the
stronger boundary. Install-time hardware facts (`total_vents_count`,
`has_bypass_damper`), free-text notes, and display names are excluded too — they
are not automation targets.

---

## Using it from an automation

With discovery on, just pick the entity:

```yaml
automation:
  - alias: "Hold the office at 68 during meetings"
    triggers:
      - trigger: state
        entity_id: binary_sensor.meeting_in_progress
        to: "on"
    actions:
      - action: number.set_value
        target:
          entity_id: number.plenum_office_hold_temperature
        data:
          value: 68
```

Or publish to a topic directly, which is handy for addressing a room by name
(or when discovery is off):

```yaml
      - action: mqtt.publish
        data:
          topic: plenum/room/office/hold/set
          payload: "68"
```

> **A hold set over MQTT always uses the REST defaults.** The payload — entity
> or raw topic alike — is just the temperature, so every MQTT hold runs for
> the default **2 hours** with `respect_eco` off (Eco Mode never relaxes it,
> #419). A custom duration (up to the 8 h cap, #576) or the Eco opt-in needs
> the web UI, REST, or MCP — see [temperature holds](./temperature-holds.md).

### Confirming a command worked

Every command publishes a result — **on every attempt, success or failure** —
to the same topic with `/result` appended:

```
plenum/room/office/hold/set          →  plenum/room/office/hold/set/result
```

```json
{"ok": true}
{"ok": false, "error": "target_temp must be between 40 and 90°F"}
```

This matters more than it looks. Plenum validates writes (schedules cannot
overlap, setpoints have bounds, pre-cool needs an outside sensor), so a command
*can* be refused — and an automation that assumed otherwise would quietly do
nothing. Subscribe to the result topic when a command needs to be reliable.
Results are **not** retained, so a new subscriber never replays an old verdict.

---

## Topic tree

Every topic starts with the instance prefix (`plenum` unless you changed it).

```
<prefix>/status                                    online | offline (retained)

<prefix>/room/<room>/<control>/set                 command
<prefix>/room/<room>/<control>/clear               command (nullable / hold)
<prefix>/room/<room>/<control>/state               current value (retained)
<prefix>/room/<room>/schedule/<schedule_id>/set    ON | OFF
<prefix>/room/<room>/presence/clear                clear presence holdover
<prefix>/room/<room>/hold/set                      hold temperature

<prefix>/thermostat/<entity>/<control>/set
<prefix>/thermostat/<entity>/<control>/state

<prefix>/system/enabled/set                        ON | OFF
<prefix>/system/vacation_mode/set                  OFF only (see below)
<prefix>/system/vacation_mode/return_at/set        ISO-8601 datetime
```

`<entity>` is the thermostat's Home Assistant `entity_id` with dots replaced:
`climate.upstairs` → `climate_upstairs`.

### Addressing a room by id or by name

`<room>` accepts **either** the room's id **or** its name:

```
plenum/room/3f2a…-9c1b/hold/set        by id
plenum/room/living_room/hold/set       by name
```

The name form is the sanitised name — lower-cased, with anything outside
`[a-z0-9_-]` collapsed to `_`. "Living Room" becomes `living_room`.

Both forms carry retained state, and a result echoes back on whichever form the
command used. Because the sanitised name has to be unambiguous, **room names
must be unique once sanitised** — "Office" and "office" now collide, and the
Rooms page will say so. On upgrade, any existing collisions are renamed
automatically (`"Office"` → `"Office (2)"`) and each rename is recorded in the
event log.

Renaming a room moves its name topics: the old ones are cleared and the new ones
published. **Discovered Home Assistant entities are unaffected** — they are keyed
on the room id, so a rename never orphans an entity or breaks an automation that
uses one. Automations that publish to a *name* topic do need updating.

### Payloads

Plain values, one per topic:

| Kind | Payload |
|---|---|
| Switch | `ON` / `OFF` |
| Number | a bare number, e.g. `68` or `1.5` |
| Select | the option name, e.g. `off_schedule_only` |
| Datetime | ISO-8601, e.g. `2026-08-01T18:00:00-04:00` |
| Button | anything (the payload is ignored) |

**Temperatures are in your display unit**, not always °F. If Plenum is showing
°C, publish `20`, not `68` — the same value you would type into the web UI.
State topics report in the display unit too.

### Clearing a value

Some settings mean "inherit" when unset — a room's `deadband_override` falls back
to its thermostat's deadband, and each per-room Eco override falls back field by
field. Two ways to clear one:

```
plenum/room/office/deadband_override/set     payload: ""    (empty)
plenum/room/office/deadband_override/clear   payload: any
```

The `/clear` topic exists because Home Assistant's number entity has no way to
express "unset" — it is published as a **Clear** button so the automation UI can
reach it.

Their `/state` topics report the **effective** value in use, so a room inheriting
its thermostat's deadband reports that number rather than a blank. An automation
cannot tell "explicitly set" from "inherited" by reading state alone.

### Two commands that only turn things off

Vacation mode and Eco Suspend both need an end time before they can be switched
on, so their switches handle `OFF` and reject a bare `ON`:

```
plenum/system/vacation_mode/return_at/set        2026-08-14T17:00:00-04:00   ← enables
plenum/system/vacation_mode/set                  OFF                         ← disables
plenum/system/vacation_mode/set                  ON                          ← rejected
```

The rejection explains what to set instead, on the result topic. Same for
`thermostat/<entity>/eco_suspend_until` (suspends) versus
`thermostat/<entity>/eco_suspend` (`OFF` clears it).

---

## Behaviour notes

- **Retained state.** Discovery configs and `/state` topics are retained, so
  Home Assistant sees current values immediately after a restart. Results are
  not retained.
- **Do not publish commands with `retain: true`.** A retained command would be
  replayed by the broker on every reconnect, re-applying it after every restart
  forever — so Plenum ignores retained messages on command topics and clears
  them from the broker instead of executing them.
- **Stale topics are cleaned up.** Shortly after connecting, Plenum retires any
  retained state or discovery config left behind by an earlier run — a room
  deleted or renamed while the bridge was disconnected does not linger as a
  ghost entity in Home Assistant.
- **Availability.** `<prefix>/status` is a Last Will topic: `online` while
  connected, `offline` on a clean shutdown *or* an ungraceful one. Discovered
  entities go unavailable rather than showing stale values if Plenum dies.
- **Successful MQTT commands are logged** to the event log, exactly like a UI
  or API change — the Logs page shows what changed and when. A *rejected*
  command reports only on its result topic, the same way a rejected UI edit
  shows an inline error rather than a log entry.
- **No rate limiting.** Commands go through the same validation as REST; there is
  no MQTT-specific throttling.
- **Authentication does not extend to MQTT.** Plenum's `require_auth` gates the
  web UI and MCP; MQTT access is whatever your broker allows. Use broker ACLs to
  restrict who can publish to Plenum's topics.

---

## Troubleshooting

**The bridge will not turn on.** The Settings card says no broker was found.
On Home Assistant OS that means the Supervisor had no MQTT service to offer —
install/start the **Mosquitto broker** add-on and restart Plenum. On standalone
Docker, set `MQTT_HOST`. The card shows the last connection error when there is
one.

**No entities in Home Assistant.** Confirm `mqtt_discovery` is on, and that
`mqtt_discovery_prefix` matches the discovery prefix your HA MQTT integration
uses (`homeassistant` unless you changed it there).

**Every entity appeared except the two datetime ones.** *Vacation Return At*
and *Eco Suspend Until* need Home Assistant 2026.5 or newer — older versions
have no MQTT `datetime` platform and drop those two configs silently.

**A command does nothing.** Subscribe to its `/result` topic — a rejected write
says why. `mosquitto_sub -h <broker> -t 'plenum/#' -v` shows the whole tree.

**Stable and beta are fighting.** Both installs default their prefix to their
add-on slug (fetched from the Supervisor at startup), so this should not happen
on HAOS — if it does, the startup log says the slug lookup failed. On standalone
Docker there is no slug: set `MQTT_TOPIC_PREFIX` on at least one of them. The
Settings card warns whenever the prefix fell back to the shared default.

---

## See also

- [MCP server](./mcp.md) — the other programmatic surface, for LLM clients
- [Authentication](./auth.md) — why the safety settings stay off MQTT
- [Schedules](./schedules.md) — schedule names, which become MQTT entity names
- [Rooms & zones](./rooms-and-zones.md) — the room settings exposed here
