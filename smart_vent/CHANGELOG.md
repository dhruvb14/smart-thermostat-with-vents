# Changelog

## 0.7.2

### Added

- Release v0.7.1 ([#95](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/95))
- Feature/addon config data migration ([#96](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/96))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.7.1

### Added

- Release v0.7.0 ([#91](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/91))
- docs: fix HAOS data directory path and Docker volume warning ([#93](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/93))
- feat: migrate data dir to addon_config for Samba visibility ([#92](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/92), [#94](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/94))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.7.0

### Added

- Release v0.6.4 ([#88](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/88))
- Rename project to Plenum; migrate flair.db → app.db ([#89](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/89), [#90](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/90))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## Unreleased

### Added

- **Metrics page** (Issue [#85](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/85)) — new `/Metrics` route with 14 charts covering heating/cooling hours, cycles per day, average duration, duty cycle, time-to-target, completion rate donut, source breakdown donut, cycles vs outside temperature, degree-minutes, overshoot histogram, per-room heating/cooling, room participation rate, hour-of-day heatmap, and a cycle-boundary vent timeline.
- **Outside-temperature capture** — record `outside_temp_at_start`/`outside_temp_at_end` on every cycle (new nullable columns on `cycle_logs`). Pick the source HA entity from the metrics page; °C → °F conversion is automatic.
- **Daily + monthly metric rollups** — APScheduler jobs aggregate completed cycles into `daily_thermostat_metrics` (00:05 local) and `monthly_thermostat_metrics` (00:10 on the 1st). Manual triggers at `POST /api/metrics/rollup/{daily,monthly}`.
- **Metrics API** — read endpoints under `/api/metrics/thermostats/{id}/...` plus `/summary` (home aggregate), `/timeseries`, `/rooms`, `/cycles-vs-outside-temp`, `/hour-heatmap`, `/vent-timeline`, `/overshoot-histogram`, `/live`, and CSV export at `/api/metrics/export.csv`.

### Changed

- **Renamed:** project is now **Plenum**. Add-on display name, panel title, browser title, and UI brand updated. Internal slug unchanged — no action required on upgrade.
- **Renamed:** on-disk database file from `flair.db` to `app.db`. First boot after this upgrade performs the rename automatically; no manual steps.
- **Bundle:** the metrics page is code-split, so the rest of the app no longer pays the recharts download cost on first paint (`/metrics` lazy-loads its chunk).

---


## 0.6.4

### Added

- Release v0.6.3 ([#84](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/84))
- Fix cycle not terminating when schedule-triggered rooms drift past target after vent close ([#86](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/86), [#87](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/87))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.6.3

### Added

- Release v0.6.2 ([#80](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/80))
- fix: close idle-room vents via control_method dispatcher (#82) ([#82](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/82), [#83](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/83))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.6.2

### Added

- Release v0.6.1 ([#78](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/78))
- fix: close idle-room vents when restoring a running cycle after reboot ([#79](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/79))
### Contributors

- Claude
- Dhruv Bhavsar
- github-actions[bot]

---


## 0.6.1

### Added

- Release v0.6.0 ([#76](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/76))
- fix: schedule preempts running presence cycle + restore room-status countdowns ([#77](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/77))
### Contributors

- Claude
- Dhruv Bhavsar
- github-actions[bot]

---


## 0.6.0

### Added

- Release v0.5.11 ([#73](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/73))
- Fix blank screen when accessed via Home Assistant ingress ([#74](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/74), [#75](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/75))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.5.11

### Added

- Fix timestamp handling: migrate holdover times to UTC and use UTC consistently ([#66](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/66))
- Release v0.5.10 ([#72](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/72))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.5.10

### Added

- Release v0.5.9 ([#69](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/69))
- Fix: Require reaching exact target temp without deadband to end cycles ([#70](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/70), [#71](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/71))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.5.9

### Added

- Release v0.5.8 ([#62](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/62))
- test: integration tests with a mock Home Assistant (#63) ([#64](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/64))
- fix: close idle room vents when a new cycle starts (issue #67) ([#67](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/67), [#68](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/68))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.5.8

### Added

- Release v0.5.7 ([#59](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/59))
- feat: cycle history diagnostics — trigger/temp/vent/setpoint capture … ([#61](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/61))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.5.7

### Added

- Release v0.5.6 ([#56](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/56))
- fix: per-vent control_method + surface vent errors to UI (#57) ([#57](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/57), [#58](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/58))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.5.6

### Added

- Release v0.5.5 ([#53](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/53))
- fix: terminate and re-evaluate cycles on every system/dev toggle ([#54](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/54), [#55](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/55))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.5.5

### Added

- Release v0.5.4 ([#50](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/50))
- fix: close cycle DB record before fallible vent/setpoint ops (#51) ([#51](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/51), [#52](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/52))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.5.4

### Added

- Release v0.5.3 ([#47](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/47))
- fix(engine): HVAC cycle engine audit — mode, setpoint, and duplicate cycle bugs ([#48](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/48), [#49](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/49))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.5.3

### Added

- Release v0.5.2 ([#41](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/41))
- ci: fix contributor lookup — prevent 422 JSON leaking into CHANGELOG ([#43](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/43), [#44](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/44))
- ci(lint): add ruff linting and formatting enforcement (issue #10) ([#10](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/10), [#45](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/45))
- ci: enforce frontend linting and formatting with ESLint and Prettier ([#13](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/13), [#46](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/46))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.5.2

### Added

- Release v0.5.1 ([#37](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/37))
- fix(engine): correct HVAC mode when ambient contradicts room sensors … ([#40](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/40))
### Contributors

- Dhruv Bhavsar
- [@{"message":"Validation Failed","errors":[{"message":"None of the search qualifiers apply to this search type.","resource":"Search","field":"q","code":"invalid"}],"documentation_url":"https://docs.github.com/v3/search/","status":"422"}](https://github.com/{"message":"Validation Failed","errors":[{"message":"None of the search qualifiers apply to this search type.","resource":"Search","field":"q","code":"invalid"}],"documentation_url":"https://docs.github.com/v3/search/","status":"422"})

---


## 0.5.1

### Added

- Release v0.5.0 ([#34](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/34))
- fix/feat: cycle engine reliability — bugs #32, startup restore, reconciliation, duplicate cycles ([#36](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/36))
### Contributors

- Dhruv Bhavsar
- [@{"message":"Validation Failed","errors":[{"message":"None of the search qualifiers apply to this search type.","resource":"Search","field":"q","code":"invalid"}],"documentation_url":"https://docs.github.com/v3/search/","status":"422"}](https://github.com/{"message":"Validation Failed","errors":[{"message":"None of the search qualifiers apply to this search type.","resource":"Search","field":"q","code":"invalid"}],"documentation_url":"https://docs.github.com/v3/search/","status":"422"})

---


## 0.5.0

### Added

- Release v0.4.2 ([#31](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/31))
- fix(engine): full audit — vent timing, thermostat setpoint, and min/max bounds correctness (issue #32) ([#33](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/33))
### Contributors

- Dhruv Bhavsar
- [@{"message":"Validation Failed","errors":[{"message":"None of the search qualifiers apply to this search type.","resource":"Search","field":"q","code":"invalid"}],"documentation_url":"https://docs.github.com/v3/search/","status":"422"}](https://github.com/{"message":"Validation Failed","errors":[{"message":"None of the search qualifiers apply to this search type.","resource":"Search","field":"q","code":"invalid"}],"documentation_url":"https://docs.github.com/v3/search/","status":"422"})

---


## 0.4.2

### Added

- Release v0.4.1 ([#28](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/28))
- fix(engine): resolve heat_cool oscillation and stale setpoint gaps (issue #29) ([#29](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/29), [#30](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/30))
### Contributors

- Dhruv Bhavsar
- [@{"message":"Validation Failed","errors":[{"message":"None of the search qualifiers apply to this search type.","resource":"Search","field":"q","code":"invalid"}],"documentation_url":"https://docs.github.com/v3/search/","status":"422"}](https://github.com/{"message":"Validation Failed","errors":[{"message":"None of the search qualifiers apply to this search type.","resource":"Search","field":"q","code":"invalid"}],"documentation_url":"https://docs.github.com/v3/search/","status":"422"})

---


## 0.4.1

### Added

- fix(#1): Support sensor-only (ventless) rooms ([#1](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/1), [#4](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/4))
- feat(#2): State drift correction + open vents on cycle termination ([#2](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/2), [#6](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/6))
- feat(#3): logging retention, time-window filtering, level filter, load more, clear logs ([#3](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/3), [#7](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/7))
- feat(#8): CHANGELOG.md, v0.4.0 version bump, tag-triggered release workflow ([#8](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/8), [#9](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/9))
- Fix cycle mode detection and vent state on abort (#26) ([#26](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/26), [#27](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/27))
### Contributors

- Dhruv Bhavsar

---


## 0.4.0

### Added

- **Logging improvements** — configurable retention periods for event and cycle logs, time-window presets (1 h / 6 h / 24 h / 7 d) with custom date-range picker, independent multi-select level filter chips (info / warning / error), offset-based load-more pagination, clear-logs button with confirmation modal, and a dedicated Retention settings tab ([#3](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/3), [#7](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/7))
- **State drift correction** — periodic reconciliation of vent positions and thermostat setpoint against actual Home Assistant state corrects any external changes; vents now re-open automatically when a cycle terminates ([#2](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/2), [#6](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/6))
- **Sensor-only room support** — rooms with no vents (monitor-only / sensor-only) no longer block cycle termination; UI label updated to "Sensor-only room" ([#1](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/1), [#4](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/4))
- **CI safety** — Docker build workflow split into a build-only job (PRs, no registry credentials) and a build-and-push job (main/tags only), preventing accidental image publishes from forks or PRs ([#5](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/5), [#4](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/4))

### Contributors

- [@dhruvb14](https://github.com/dhruvb14)

---

## 0.3.0

- Initial public release: room scheduling, presence holdover, developer mode, live room status cards, schedule countdown timers, and backup/restore support
