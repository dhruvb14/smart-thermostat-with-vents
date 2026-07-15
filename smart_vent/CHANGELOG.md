# Changelog

## 0.31.0

### Added

- chore(deps): bump ws from 8.20.0 to 8.21.0 in /smart_vent/frontend in the npm_and_yarn group across 1 directory ([#462](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/462))
- Authentication for the web UI & MCP server (#373, Phases 1–5) ([#463](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/463))
- Add Beta release track: prebuilt-image add-on auto-built on every main merge ([#466](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/466))
- fix(beta.yml): valid YAML for the version-stamp step (unblock the beta workflow) ([#467](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/467))
- beta: write the changelog inside each PR; build the image only on push to main ([#468](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/468))
- fix(auth+beta): X-Supervisor-Token login + monotonic beta version (#373) ([#469](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/469))
- Dedicated Settings page: MCP server + tokens + Backup/Restore, fix cramped MCP modal (#471) ([#471](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/471), [#472](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/472))
- chore(Increment Beta Version) ([#473](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/473))
- Document OIDC/OAuth2 auth-proxy option for cloud MCP connectors ([#474](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/474))
- Add OIDC single sign-on for the web UI (#464) ([#464](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/464), [#476](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/476), [#475](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/475))
### Contributors

- Dhruv Bhavsar
- dependabot[bot]
- github-actions[bot]

---


## 0.30.0

### Added

- chore(deps): consolidate Dependabot updates ([#455](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/455))
- De-flake Rooms/Room-detail goldens: pin backend clock for the status read path + seed all status permutations ([#457](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/457))
- Mobile usability: reflow wide tables into cards and fix non-collapsing layouts (M1–M5 of #458) ([#459](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/459))
- Dark mode: theme tokens, persisted light/dark/system setting, dual-theme goldens (#458) ([#460](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/460))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.29.0

### Added

- Eco Mode: fractional relaxed targets with directional whole-degree setpoint commands; Logs eco transparency; seeded Logs goldens ([#445](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/445), [#446](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/446))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.28.0

### Added

- Metrics: Eco Mode impact section + derived charts, with charts rendered deterministically in E2E goldens ([#442](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/442), [#443](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/443))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.27.1

### Added

- Make Clear Presence stick while the room is still occupied (#439) ([#439](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/439), [#440](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/440))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.27.0

### Added

- Close out the review backlog: eco engine fixes, restart/restore hardening, differential tests, CI efficiency (17 issues, one commit each) ([#408](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/408), [#409](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/409), [#410](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/410), [#412](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/412), [#413](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/413), [#414](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/414), [#415](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/415), [#416](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/416), [#417](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/417), [#418](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/418), [#419](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/419), [#420](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/420), [#427](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/427), [#428](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/428), [#429](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/429), [#430](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/430), [#431](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/431), [#432](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/432), [#433](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/433), [#434](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/434), [#437](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/437))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.26.0

### Added

- Vent-layer and compressor-protection fixes: zone-wide airflow floor, overflow-vs-reconcile, hold release, parked setpoint, method-aware vent control, lockout coverage ([#421](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/421), [#422](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/422), [#423](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/423), [#424](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/424), [#425](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/425), [#426](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/426), [#435](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/435))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.25.0

### Added

- Make metrics & cycle-log APIs date-range queryable (start/end/days, offset) over REST + MCP (#403) ([#403](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/403), [#405](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/405))
- Eco Mode — outdoor-temperature-compensated setpoint drift ([#402](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/402), [#404](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/404), [#406](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/406))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.24.0

### Added

- Versioned DB schema migrations with automatic pre-migration backup ([#21](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/21), [#400](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/400))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.23.1

### Added

- Use the Plenum logo for the iOS Add to Home Screen icon ([#398](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/398))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.23.0

### Added

- MCP port: revert to null (opt-in) and document that an add-on Update detects it ([#387](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/387))
- Add .claude/skills/ continuity library (16 skills), fix CLAUDE.md drift, and wire CLAUDE.md to the skills ([#388](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/388))
- Skill library review pass: fix truncated skill descriptions, wrong facts, and finish the drift scrub ([#389](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/389))
- Show requested temp and clear-presence control on Dashboard active rooms ([#390](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/390))
- Fix README stats/docs drift and system-modes precedence doc ([#395](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/395))
- Fix degree-minutes chart reading samples the engine never writes (#394) ([#394](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/394), [#396](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/396))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.22.1

### Added

- chore(deps): consolidate dependabot PRs ([#384](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/384))
- Map the MCP port to a default host port so it shows in the add-on Network config ([#385](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/385))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.22.0

### Added

- Serve MCP over HTTP on a dedicated port with a UI toggle ([#375](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/375))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.21.0

### Added

- ci: move visual regression e2e into container-ci with parallel legs and fan-in golden commit ([#366](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/366))
- Enforce comfort envelope when not in vacation mode (#367) ([#367](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/367), [#368](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/368))
- ci: fix commit-goldens push from detached HEAD (#369) ([#369](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/369), [#370](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/370))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.20.0

### Added

- Restyle vacation mode banner as a green card ([#363](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/363), [#364](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/364))
### Contributors

- Dhruv Bhavsar
- github-actions[bot]

---


## 0.19.0

### Added

- chore(deps): update aioresponses requirement from >=0.7.8 to >=0.7.9 in /smart_vent ([#351](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/351))
- Schedule copy, enable/disable, and self-expiring (temporary) schedules (#359) ([#359](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/359), [#361](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/361))
### Contributors

- Claude
- Dhruv Bhavsar
- github-actions[bot]

---


## 0.18.0

### Added

- Miscellaneous improvements and fixes
### Contributors

- Claude
- dependabot[bot]
- github-actions[bot]

---


## 0.17.0

### Added

- Fix metric-HA support, MCP startup crash, and WebSocket reconnect leak (#280–#283) ([#280](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/280), [#306](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/306))
- chore(deps): bump esbuild from 0.25.12 to removed in /smart_vent/frontend in the npm_and_yarn group across 1 directory ([#307](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/307))
- Convert MCP write-tool temperatures from display unit to °F (#284) ([#284](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/284), [#308](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/308))
- Abort in-flight cycle when a thermostat's engine is removed (#285) ([#285](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/285), [#309](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/309))
- Retry transient _tick_engine failures instead of silently dropping the zone (#286) ([#286](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/286), [#310](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/310))
- Fix event_log trim off-by-one that drops oldest row below cap (#299) ([#299](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/299), [#311](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/311))
- Gate per-room time-to-target on in-range cycle join (#289) ([#289](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/289), [#312](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/312))
- Apply overshoot histogram thermostat fallback per (cycle, room) (#290) ([#290](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/290), [#313](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/313))
- Preserve role/joined_at on room_cycle_states upsert and stop overflow state leaks (#300) ([#300](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/300), [#314](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/314))
- Skip idle setpoint-to-ambient reset when already at ambient (#296) ([#296](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/296), [#315](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/315))
- Honor per-room deadband_override in overflow candidate selection (#305) ([#305](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/305), [#316](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/316))
- Refresh presence holdover for continuously-on occupancy sensors (#287) ([#287](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/287), [#317](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/317))
### Contributors

- Claude
- Dhruv Bhavsar
- dependabot[bot]
- github-actions[bot]

---


## 0.16.0

### Added

- Abort cycles on sustained thermostat unavailability; close critical engine test gaps ([#267](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/267), [#268](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/268), [#269](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/269), [#270](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/270), [#271](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/271))
- chore(deps): bump @types/node from 25.9.1 to 25.9.2 in /smart_vent/frontend ([#272](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/272))
- chore(deps): bump prettier from 3.8.3 to 3.8.4 in /smart_vent/frontend ([#273](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/273))
- chore(deps): update aiohttp requirement from >=3.14.0 to >=3.14.1 in /smart_vent ([#274](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/274))
- chore(deps): bump typescript-eslint from 8.60.1 to 8.61.0 in /smart_vent/frontend ([#275](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/275))
- chore(deps): update ruff requirement from >=0.15.15 to >=0.15.16 in /smart_vent ([#276](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/276))
- Per-room deadband override (#277) ([#277](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/277), [#278](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/278))
### Contributors

- Dhruv Bhavsar
- dependabot[bot]
- github-actions[bot]

---


## 0.15.0

### Added

- fix(logs): Apply button for custom date filter + contained feed auto-scroll ([#263](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/263), [#264](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/264))
- docs: update Tested section with current stats ([#265](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/265))
### Contributors

- Dhruv Bhavsar
- dependabot[bot]
- github-actions[bot]

---


## 0.14.1

### Added

- chore(deps): bump vitest from 3.2.4 to 4.1.8 in /smart_vent/frontend in the npm_and_yarn group across 1 directory ([#249](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/249))
- Record overflow-conditioned rooms as cycle data points (Issue #254) ([#254](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/254), [#255](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/255))
### Contributors

- Dhruv Bhavsar
- dependabot[bot]
- github-actions[bot]

---


## 0.14.0

### Added

- Raise frontend and backend test coverage ([#247](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/247))
- Consolidate temperature conversion into backend/units.py ([#251](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/251), [#252](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/252))
### Contributors

- Claude
- Dhruv Bhavsar
- github-actions[bot]

---


## 0.13.1

### Added

- Add add-on icon and logo for HACS/Supervisor display ([#241](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/241))
- Potential fix for code scanning alert no. 4: Information exposure through an exception ([#242](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/242))
- Add Screenshots ([#243](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/243))
- Fix #244: _terminate_cycle reopens idle-room vents; clarify idle reconciler warning ([#245](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/245))
### Contributors

- Claude
- Dhruv Bhavsar
- github-actions[bot]

---


## 0.13.0

### Added

- chore(deps): update pytest-asyncio requirement from >=1.3.0 to >=1.4.0 in /smart_vent ([#234](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/234))
- chore(deps): update ruff requirement from >=0.15.13 to >=0.15.14 in /smart_vent ([#235](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/235))
- chore(deps): bump typescript-eslint from 8.59.4 to 8.60.0 in /smart_vent/frontend ([#236](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/236))
- Fix #237: hold-state gate prevents vent thrashing + overflow conditioning ([#237](https://github.com/dhruvb14/smart-thermostat-with-vents/issues/237), [#238](https://github.com/dhruvb14/smart-thermostat-with-vents/pull/238))
### Contributors

- Dhruv Bhavsar
- dependabot[bot]
- github-actions[bot]

---


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
