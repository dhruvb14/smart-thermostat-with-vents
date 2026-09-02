# Temperature holds

A hold pins **one room at one exact temperature for a fixed time** — warm a
nursery for a nap, cool an office ahead of a meeting, shake off a temperature
swing — without editing the room's schedules or presence settings (#576). It is
the manual override the engine has always resolved first, now with a shared UI
on three pages, a hard time cap, and an event-log trail.

> **Scope:** a hold is per-room and temporary. Pick a preset duration of
> **1, 2, 4, 6, or 8 hours** — 8 h is a hard cap enforced by the API. A hold is
> temporary relief, not a standing setting: when the time is up it deletes
> itself and the room falls straight back to schedule/presence control.

## How a hold behaves

- **It wins.** While a hold is live it is the room's active source — the
  priority order is **override > schedule > presence** (see
  [schedules](./schedules.md)). Nothing else about the room is changed, and a
  hold is never conflict-checked against schedule blocks.
- **It applies immediately.** Setting or cancelling a hold re-resolves the
  zone right away — the API kicks the room's cycle engine instead of waiting
  for the next 60 s tick.
- **Replacing is in-place.** Posting a hold for a room that already has one
  replaces it, and a mid-cycle change — a new target, or just a flipped Eco
  opt-in — is applied to the running cycle **in place**, with no compressor
  stop/start (#215).
- **Expiry deletes; schedule expiry only disables.** When a hold expires, the
  engine deletes it on the next tick and logs the handoff. Contrast with a
  schedule's **Auto-disable at** expiry (#359), which parks the block so it can
  be re-enabled later: a block is a standing setting worth keeping; a hold has
  no later, so nothing lingers.

| Field | Default | What it does |
|---|---|---|
| **Hold temperature** | 72 °F (or the existing hold's target) | The exact target the room runs to. Must be between **40 and 90 °F**. |
| **Hold for** | **2 hours** | Preset durations 1 / 2 / 4 / 6 / 8 h. The API accepts any `duration_hours` greater than 0 and at most 8. |
| **Allow Eco Mode to relax this hold** | off | The `respect_eco` opt-in — see below. |

## Eco Mode and holds — an opt-in

By default a hold **ignores [Eco Mode](./eco-mode.md)**. An explicit "this
room, this temperature, right now" is the strongest user signal there is, so
Eco never relaxes it (#419) — that behaviour is unchanged, and every hold set
before this option existed keeps it on upgrade.

Tick **Allow Eco Mode to relax this hold** (`respect_eco`) to opt a single
hold in: on extreme days Eco may then relax the hold's target exactly like a
scheduled room's. Two things still prevent relaxation even for an opted-in
hold: an active [Eco Suspend](./eco-mode.md#temporarily-suspending-eco-eco-suspend-issue-500)
on the thermostat, and a per-room Eco configuration that resolves to **Off**.
Flipping the flag on a live hold (**Replace hold**) takes effect on the
running cycle in place — the same mid-cycle update mechanism as the
per-schedule deadband (#215, #517).

## Where the UI lives

One shared modal — room picker, temperature, duration, the Eco checkbox —
opens from three places:

- **Dashboard** — the page-level **🕒 Temporary hold** button (reading
  "*N* holds active — manage" while any are live). Every live hold gets a
  strip row under the header — room, target, countdown, whether it ignores
  Eco — with a **Cancel** button, and a held room carries a purple **Hold**
  badge on its zone card.
- **Rooms** — each card's bottom row has a **Hold** (or **Manage hold**)
  button; while a hold is driving the room, the status row shows the Eco tag
  and a **Cancel hold** button.
- **Schedules** — a held room's expanded section shows a **Temporary hold**
  card above the block table (target, countdown, Eco tag, **Manage hold** and
  **Cancel hold**); when there is no hold, the footer offers
  **🕒 Set temporary hold**.

## Event log

Every hold transition is visible on the [Logs page](./observability.md):

- **Set** — `Temperature hold set for room …`, with the target, duration, and
  whether Eco may relax it.
- **Cancelled** — `Temperature hold cancelled for room …`. Logged only when a
  live hold actually existed; cancelling nothing is a silent no-op.
- **Expired** — `Temperature hold expired for … — resuming schedule/presence
  control`, logged by the engine on the tick that sweeps the hold away.

## API

| Call | What it does |
|---|---|
| `POST /api/rooms/{room_id}/override` | Set or replace the room's hold. Body: `target_temp` (in the display unit; must land in 40–90 °F once stored), `duration_hours` (default `2`; must be greater than 0 and at most 8), `respect_eco` (default `false`). `404` for an unknown room. |
| `DELETE /api/rooms/{room_id}/override` | Cancel the hold. Idempotent — deleting when no hold exists still returns `cleared`. `404` for an unknown room. |
| `GET /api/overrides` | Every live hold: `room_id`, `target_temp` (raw °F), `expires_at` (naive-UTC ISO), `respect_eco`, `ends_in_seconds`. |

- **[MQTT](./mqtt.md)** — the per-room **Hold Temperature** entity (and its
  raw `…/hold/set` topic) carries only a temperature, so an MQTT hold always
  takes the REST defaults: the 2-hour duration and `respect_eco` off.
- **[MCP](./mcp.md)** — the built-in MCP server exposes the endpoints above
  as tools dispatched through REST, so `respect_eco`, the 8 h cap, and the
  immediate effect all carry over unchanged. (The standalone stdio server's
  `set_room_override` tool also accepts `respect_eco` and enforces the same
  cap, but writes the hold directly, so it applies on the next engine tick —
  within ~60 s — rather than instantly.)

## See also

- [Schedules](./schedules.md) — the standing per-room time blocks a hold outranks
- [Presence & motion](./presence.md) — the other activation source a hold outranks
- [Eco Mode](./eco-mode.md) — what the `respect_eco` opt-in hands the hold over to
- [Observability](./observability.md) — the event log where hold transitions land
