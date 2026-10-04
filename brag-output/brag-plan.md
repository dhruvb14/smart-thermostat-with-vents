# Plenum — brag plan

**What:** A Home Assistant add-on that turns one thermostat + smart vents into room-by-room HVAC zoning.
**For:** HA homeowners with smart vents (Flair or any `cover.*`) whose rooms never agree with the hallway thermostat.
**Sets it apart:** Rooms request temps (schedule / presence); Plenum opens only their vents, pushes the setpoint past the hardest room, closes each vent the moment that room hits target — with real equipment-safety guards.
**Hook:** "Your thermostat says 70°F." — then the rooms around it reading 74.2 / 66.8 / 72.9 / 67.4.
**Share caption:** One thermostat, every room at its own temperature.

**Tone:** default — punchy, clean, soft transitions. **Identity:** Plenum's own dark theme tokens (#0f172a bg, #1e293b surface, #3b82f6 blue, #22c55e vent-open green), system-ui, 🌡 brand mark, real `styles.css` + real Dashboard zone-card markup.

## Storyboard (21s, 1920×1080, 30fps)
| # | Time | Scene |
|---|---|---|
| 1 | 0.0–3.4 | Hook: big hallway thermostat "70°F" + "Your thermostat says 70°F." Room tiles pop in around it with the real temps they're at. |
| 2 | 3.4–6.6 | Reveal: 🌡 Plenum, "Room-by-room HVAC zoning for Home Assistant." |
| 3 | 6.6–13.0 | The product: the real Dashboard zone card. Bedroom + Office gain PRESENCE and start requesting; cycle goes Cooling, setpoint overshoots to 66°F, vents open; temps fall, each vent closes as its room hits target, progress fills, cycle idles. Captions: "Rooms ask." / "Vents open where it's needed." / "And close the moment it's done." |
| 4 | 13.0–17.0 | Safety: "It drives real equipment, so it's careful." + Short-cycle protection · Airflow floor (⅓ of vents stay open) · Cooling lockout when it's cold out. |
| 5 | 17.0–21.0 | Outro: 🌡 Plenum · "Any cover.* vent. Any climate.* thermostat." · Home Assistant add-on · repo URL. |

Sound: soft D-major pad + gentle pulse; pops/ticks are plucked notes in key, sitting under the music.
