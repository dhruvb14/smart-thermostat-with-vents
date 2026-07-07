# Pre-cool / pre-heat (ambient-aware presence suppression)

When someone walks into a room, presence normally drives the HVAC to the
room's presence target. But if the **outside air** will carry the room to that
target on its own, running the heater or AC just wastes energy (and the
overnight "coolness" you banked). This per-room feature lets a presence-active
room **coast** to its target on ambient drift instead of conditioning — a
software version of the "pre-cool/pre-heat" idea many smart thermostats offer.

It is **off by default**, configured **per room**, and only works when an
**outside temperature sensor** is configured — the home-wide *Outside
temperature sensor* picker at the top of the **Thermostats** page (the same
sensor the [cooling lockout](./safety.md) uses). Without a readable outside
sensor the feature is inert and presence behaves exactly as before — a
deliberate fail-safe.

## The problem it solves

> A night schedule holds the bathroom at **68°F** and ends at 7:00am. At 7:30am
> someone walks in and presence wants **70°F**, so the heater runs to add 2°F.
> But it is already warmer than 70°F outside — the room will drift past 70°F on
> its own as the sun comes up. The heat (and the stored overnight coolness) is
> wasted.

With the feature enabled on that room, the morning presence event does **not**
call for heat: the room is left to drift up to 70°F on ambient gain. A hard
floor still protects comfort if the room somehow gets too cold.

## Per-room settings

All of these live in the Room settings modal (**Rooms → Settings**), under
*"Skip presence heating/cooling when the weather will do it for me"*. The two
temperature fields are **deltas** shown in your active unit (°F or °C).

| Setting | Meaning |
|---|---|
| **Enable** | Master toggle. Disabled until an outside sensor is configured. |
| **When to apply** | `Any presence` (every presence activation) or `Only after a schedule ends` (just the post-schedule window — the 7am case). |
| **Schedule window (minutes)** | Only for *"Only after a schedule ends"*: how long after a block ends the feature still applies. Default 60. |
| **Minimum outside difference** | How far past the target the outside temp must be before coasting (default 5°F). |
| **Widened deadband** | The grace band used while coasting. Must be **≥ the thermostat's deadband** (default 2°F). |

### Minimum outside difference

Coasting only makes sense when the weather is genuinely on the helpful side of
the target — otherwise drift is too slow and you should just run the HVAC.

With a 70°F target and a 5°F minimum difference:

| Outside | Decision |
|---|---|
| 80°F (+10) | **Skip heat** — strong push, let it drift up |
| 71°F (+1) | **Run heat** — only 1° of push, drift would be too slow |

Concretely: heating is skipped only when `outside ≥ target + difference`, and
cooling is skipped only when `outside ≤ target − difference`. A larger value is
more conservative (coast less often) and is always safe.

### Widened deadband and the threshold-crossing rule

While coasting, the room rides a **wider** deadband than normal — but only on
the side it is coasting *from*. **The instant the room crosses its presence
target, the far side reverts to the normal deadband.** The widened band never
spans across the target.

Example — target **70°F**, normal deadband **2°F**, widened deadband **3°F**,
outside **80°F**:

| Room temp | Decision | Why |
|---|---|---|
| 68°F | **no heat** | within the widened floor (70 − 3 = 67°F); coast up |
| 66°F | **heat** | dropped below the widened floor — comfort protection |
| 70°F | idle | at target |
| 72°F | **cool to 70°F** | crossed the target → normal deadband: cool at 70 + 2 = **72°F**, *not* 73°F |

### Hard cap

Whatever the feature decides, the thermostat's **min/max setpoint** always
wins: if the room drifts to or below `min_setpoint` it heats, and at or above
`max_setpoint` it cools. Coasting can never push a room past those absolute
comfort bounds. (See [Thermostat settings](./thermostat-settings.md).)

## How the decision is made

Each tick, for every presence-active room with the feature engaged:

1. **Gate** — is the outside temp at least `min_differential` past the target on
   the helpful side? If not, run HVAC normally.
2. **Direction** — below target with warm outside → coast up; above target with
   cool outside → coast down.
3. **Widened band** — while coasting, only call for HVAC if the room leaves the
   widened band on the coasting side. Once the room crosses the target, the
   normal deadband governs the other side.
4. **Hard cap** — `min_setpoint`/`max_setpoint` override everything.

A suppressed room contributes **no demand**: if it is the only active room, no
cycle starts and its vents stay at the resting (open) position, exactly as if
presence had never fired. If **another room** drives a cycle, the coasting room
is excluded from it and its vents are **closed for the duration of that
cycle**, like any idle room's — an open vent would blow the active cycle's
supply air into the room and fight the coast. They reopen when the cycle ends.
Schedule and manual-override targets are explicit user intent and are
**never** suppressed — only presence-driven demand is eligible.

## Trigger scope

- **Any presence** — the check runs every time presence activates the room.
- **Only after a schedule ends** — the check runs only while the room is within
  the configured window after a schedule block ended (the night→morning case),
  and behaves normally the rest of the day. Overnight blocks count from their
  morning end time.

## See also

- [Presence & motion](./presence.md) — how presence activates a room (and how
  this feature can intentionally skip it)
- [Rooms & zones](./rooms-and-zones.md) — the full set of per-room settings
- [Safety features](./safety.md) — other outside-temperature-gated behavior
- [Thermostat settings](./thermostat-settings.md) — deadband, setpoint bounds
