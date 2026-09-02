# Vent control methods

Every vent stores how Plenum should actually open and close it. The right method depends on which services the vent's Home Assistant integration exposes.

## The four methods

| Method | How "open" is sent | How "close" is sent | When to use |
|---|---|---|---|
| **`open_close`** (default) | `cover.open_cover` | `cover.close_cover` | Standard covers that support the basic open/close service pair. This is what most HA cover integrations implement. |
| **`set_position`** | `cover.set_cover_position` with `position=100` | `cover.set_cover_position` with `position=0` | Vents that expose a position attribute (0–100) but don't implement the open/close shortcuts. |
| **`set_tilt_position`** | `cover.set_cover_tilt_position` with `tilt_position=100` | `cover.set_cover_tilt_position` with `tilt_position=0` | Flair vents via the [RobertD502 HACS integration](https://github.com/RobertD502/home-assistant-flair) and any other cover that reports **`current_tilt_position`** instead of `current_position`. |
| **`toggle`** | `cover.toggle` | `cover.toggle` | Stateful vents that flip between open and closed on the same service call. Use only if neither pair of services above works. |

## Picking the right method

In the UI, the vent row has a **Test** button that lets you trial a method before saving. If a vent opens but the UI keeps showing it as closed, the attribute you're reading doesn't match — try `set_tilt_position` for Flair vents, or `set_position` for standard position-based covers.

## How Plenum reads vent state

Plenum judges whether a vent is passing air using the attribute that matches its own configured `control_method` — it does not check both position attributes on every vent. A `set_tilt_position` vent is read from `current_tilt_position`; a `set_position` vent is read from `current_position`; `open_close` and `toggle` vents have no position attribute to read and fall back to HA's plain cover `state` (`open`/`closed`). If the matching attribute is present but the value can't be read, or the control method doesn't use one, Plenum also falls back to `state`. That's why `set_tilt_position` is the right choice for Flair — the integration reports tilt, not position, and picking the wrong method means Plenum is reading an attribute the vent never updates.

## What happens on failure

If a single vent fails to open or close, Plenum logs the error with the entity ID and control method and moves on. A misconfigured method on one vent does not abort the cycle or affect other vents in the room.
