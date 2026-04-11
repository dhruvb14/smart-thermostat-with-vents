# Changelog

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
