# Changelog

## 0.12.1

### Added

- Surface sensor-staleness threshold on the Thermostats page ([#230](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/230))
- Fix #231: frontend sends display-unit temperatures; backend converts ([#232](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/232))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.12.0

### Added

- chore(deps): consolidate Dependabot updates (7 PRs) ([#207](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/207))
- Add compressor short-cycle protection to the cycle engine ([#208](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/208), [#214](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/214))
- Add outdoor-temperature cooling lockout ([#209](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/209), [#216](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/216))
- chore(deps): consolidate Dependabot updates (6 PRs) ([#223](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/223))
- Guard the cycle engine from stale temperature readings ([#211](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/211), [#224](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/224))
- Apply mid-cycle trigger changes in place instead of tearing down ([#215](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/215), [#225](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/225))
- Airflow floor: replace min_open_vents with a fraction-of-total safety ([#213](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/213), [#226](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/226))
- End-to-end test for the cycle-timeout safety guard ([#212](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/212), [#227](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/227))
- Pin airflow-floor bypass invariants + surface idle-room deferral ([#210](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/210), [#228](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/228))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.11.2

### Added

- fix: HVAC never restarts after vacation mode ends ([#205](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/205))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.11.1

### Added

- fix: revert thermostat from heat_cool mode when vacation is not active ([#192](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/192))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.11.0

### Added

- feat: vacation mode — suspend schedules and hold thermostats within safety band ([#189](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/189), [#190](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/190))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.10.2

### Added

- Fix HA WebSocket disconnect causing UI to hang/become unresponsive ([#185](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/185))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.10.1

### Added

- Add "Clear presence" button to cancel accidental holdover ([#179](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/179), [#180](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/180))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.10.0

### Added

- 🛡️ Sentinel: [MEDIUM] Fix Input Validation Bounds ([#165](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/165))
- chore(deps): update setuptools requirement from >=70 to >=82.0.1 in /smart_vent ([#167](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/167))
- chore(deps): update aiohttp requirement from >=3.9 to >=3.13.5 in /smart_vent ([#168](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/168))
- chore(deps): bump typescript-eslint from 8.59.1 to 8.59.2 in /smart_vent/frontend ([#169](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/169))
- chore(deps): update pytest requirement from >=8 to >=9.0.3 in /smart_vent ([#172](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/172))
- Add E2E visual regression tests with Playwright + Docker Compose HA ([#176](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/176))
- ci: add E2E visual regression tests to Validate Release workflow ([#177](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/177))
### Contributors

- Dhruv Bhavsar
- dependabot[bot]
- github-actions[bot]

---


## 0.9.16

### Added

- Potential fix for code scanning alert no. 4: Information exposure through an exception ([#160](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/160))
- Support environment variables as fallback for Docker mode ([#163](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/163))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.9.15

### Added

- Potential fix for code scanning alert no. 6: Workflow does not contain permissions ([#155](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/155))
- fix(#150): replace OutsideTempPanel dropdown with EntityPicker search ([#150](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/150), [#156](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/156))
- fix(#151): prevent chart grid overflow on mobile viewports ([#157](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/157))
- ci(#11): add mypy static type checking, fix all 44 annotation gaps ([#158](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/158))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.9.14

### Added

- chore(deps): update mcp requirement from >=1.0 to >=1.27.0 in /smart_vent ([#143](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/143))
- chore(deps): update apscheduler requirement from >=3.10 to >=3.11.2 in /smart_vent ([#144](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/144))
- chore(deps): update pytest-asyncio requirement from >=0.23 to >=1.3.0 in /smart_vent ([#145](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/145))
- chore(deps): update ruff requirement from >=0.4 to >=0.15.12 in /smart_vent ([#146](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/146))
- chore(deps): update marshmallow-dataclass requirement from >=8.6 to >=8.7.1 in /smart_vent ([#147](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/147))
- Move system/dev toggles and API docs to settings gear dropdown with confirmation dialogs ([#149](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/149), [#153](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/153))
### Contributors

- Dhruv Bhavsar
- dependabot[bot]
- github-actions[bot]

---


## 0.9.13

### Added

- ci: release process maturity — versioning, healthz, smoke test, runbook ([#142](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/142))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.9.12

### Added

- Fix docker container dependencies issue ([#139](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/139))
### Contributors

- Dhruv Bhavsar

---


## 0.9.10

### Added

- Implement OpenAPI documentation and CI enforcement ([#130](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/130))
- Release v0.9.9 ([#137](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/137))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.9.9

### Added

- Miscellaneous improvements and fixes
### Contributors

- @dhruvb14

---


## 0.9.6

### Added

- Release v0.9.5 ([#132](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/132))
- Fix Docker container base ([#133](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/133))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.9.5

### Added

- 🛡️ Sentinel: Security hardening and input validation ([#126](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/126))
- 🛡️ Sentinel: Fix middleware RuntimeError and harden security headers ([#127](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/127))
- Release v0.9.4 ([#128](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/128))
- Add multi-platform Docker builds (amd64, arm64) ([#129](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/129))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.9.4

### Added

- Release v0.9.3 ([#122](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/122))
- 🛡️ Sentinel: Ensure security headers on 500 error responses ([#124](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/124))
- Add unit-aware temperature support (Celsius / Fahrenheit) ([#123](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/123), [#125](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/125))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.9.3

### Added

- Release v0.9.2 ([#107](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/107))
- ci: add automated dependency updates with Dependabot ([#108](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/108))
- chore(ci): bump actions/checkout from 4 to 6 ([#109](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/109))
- chore(ci): bump docker/metadata-action from 5 to 6 ([#110](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/110))
- chore(ci): bump docker/login-action from 3 to 4 ([#111](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/111))
- chore(ci): bump actions/setup-python from 5 to 6 ([#112](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/112))
- chore(ci): bump docker/setup-buildx-action from 3 to 4 ([#113](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/113))
- chore(deps): bump eslint-plugin-react-refresh from 0.4.26 to 0.5.2 in /smart_vent/frontend ([#114](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/114))
- chore(deps): bump typescript-eslint from 8.58.2 to 8.59.1 in /smart_vent/frontend ([#115](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/115))
- Enable CodeQL and Automated Issue Synchronization ([#116](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/116))
- Enforce code coverage in CI ([#117](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/117))
- Update CodeQL issue sync workflow for issue creation ([#118](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/118))
- Refactor CodeQL issue sync workflow for clarity ([#119](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/119))
- fix: replace deprecated code_scanning_alert trigger in CodeQL issue sync workflow ([#120](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/120))
- fix(ci): enforce coverage thresholds and fix summary table capture ([#121](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/121))
### Contributors

- Dhruv Bhavsar
- dependabot[bot]
- github-actions[bot]

---


## 0.9.2

### Added

- Release v0.9.1 ([#103](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/103))
- Implement UI Unit Tests and Frontend Validations (#104) ([#105](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/105))
- 🛡️ Sentinel: Add security headers middleware ([#106](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/106))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.9.1

### Added

- Release v0.9.0 ([#101](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/101))
- Fix participation count calculation in room metrics query ([#102](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/102))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.9.0

### Added

- Release v0.8.0 ([#99](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/99))
- Rename add-on from flair-replacement to plenum ([#100](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/100))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.8.0

### Added

- Release v0.7.2 ([#97](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/97))
- feat(metrics): Issue #85 Phase 1 — schema, outside-temp capture, rollup jobs ([#98](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/98))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


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
