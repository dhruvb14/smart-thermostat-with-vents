# Plenum Beta — Changelog

## 0.37.0-beta.6 — building toward v0.37.0

> ⚠️ **Beta channel.** Tracks the tip of `main` and may be unstable. For a
> production install, use the **Plenum** (stable) add-on. Everything below is
> heading for the next stable release (v0.37.0).

**Landed on beta since v0.36.0:**

- Scan the container image on every code PR, not just release PRs ([#598](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/598))
- Fix the Thermostats form reset that discarded in-progress edits (#597) ([#599](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/599))
- Gate the sensor-staleness card on its mount fetch (#600) ([#601](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/601))
- test: drive backend and frontend coverage toward 100%, and fix tests that could not fail ([#602](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/602))
- security: take the image to zero Trivy findings and gate CI on MEDIUM and above ([#610](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/610))
- fix: retry a failed room-state repair instead of leaving a phantom entry (#603) ([#611](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/611))
