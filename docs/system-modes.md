# System modes

Two global toggles control how Plenum behaves at runtime. Both are in the top-right of every page and persist across restarts.

## System On / Off

The **System On/Off** switch decides whether Plenum's cycle engine is allowed to make changes in Home Assistant.

- **On** — cycle engines tick every 60s and drive vents and thermostats normally.
- **Off** — engines do not tick, and any cycle that's running is aborted immediately (vents restored, setpoint released). Plenum still monitors state and serves the UI, but makes zero service calls to HA.

Use **Off** while you're transitioning from another HVAC control system, or any time you want Plenum to sit quietly without touching your equipment.

## Dev Mode

**Dev Mode** intercepts every HA service call that Plenum would make — `climate.set_temperature`, `cover.open_cover`, `cover.close_cover`, `cover.set_cover_position`, `cover.set_cover_tilt_position`, `cover.toggle` — and logs them to the event log instead of sending them to Home Assistant.

- **On** — the engine runs normally, picks rooms, computes vent moves, but nothing reaches HA. Check the **Logs** page to see what *would* have been sent.
- **Off** — service calls go through as usual.

Dev Mode is useful for:
- Tuning schedules, overshoot, and deadband without cycling the actual HVAC.
- Validating a fresh setup against what the engine is doing.
- Reproducing a bug without affecting the house.

**System On/Off takes precedence.** If the system is Off, Dev Mode is moot — nothing ticks at all.
