# Plenum Beta — Changelog

## 0.37.0-beta.16 — building toward v0.37.0

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
- fix: survive a wrong-shaped rooms_json snapshot at startup (#604) ([#612](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/612))
- fix: say when the log-retention form is showing fabricated defaults (#605) ([#613](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/613))
- fix: four low-priority correctness and housekeeping defects (#606, #607, #608, #609) ([#614](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/614))
- fix: restore the compressor off-time lockout on the degraded restore path, and 7 other review findings on #603/#604 ([#616](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/616))
- fix: give metrics their own retention so no log purge can destroy them (#617, #615) ([#618](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/618))
- Run per-room safety cycles during vacation mode (#626) ([#624](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/624))
- chore(deps): consolidate 5 Dependabot updates ([#625](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/625))
- chore(deps): bump js-yaml from 4.3.1 to 4.3.2 in /smart_vent/frontend in the npm_and_yarn group across 1 directory ([#629](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/629))
- fix: give the vacation hold a once-per-transition event log (#627) ([#630](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/630))
- fix: give the vacation hold hysteresis and a re-arming compressor lockout (#628) ([#631](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/631))
