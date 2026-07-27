"""Broker + topic-prefix resolution (Issue #519, reworked after HAOS field test).

The first real HAOS boot found two failures this file now pins:

* the ``mqtt_enabled`` deployment gate made a zero-config HAOS install (broker
  already provided by the Supervisor) read as "not configured", blocking the
  Settings-page toggle — so the gate is gone and resolution is unconditional;
* the add-on slug came back empty from ``bashio`` in ``run.sh``, so the beta
  install booted with the stable ``plenum`` prefix — the slug is now fetched
  from the Supervisor REST API in Python, where these tests can exercise it.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

import pytest

from backend.mqtt.config import (
    DEFAULT_DISCOVERY_PREFIX,
    DEFAULT_PORT,
    DEFAULT_PREFIX,
    _supervisor_mqtt,
    _supervisor_slug,
    load_config,
    log_resolution,
    resolve_prefix,
)


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def _fake_urlopen(body: str):
    def _open(req, timeout=None):
        return _FakeResponse(body)

    return _open


_MQTT_ENV = (
    "MQTT_HOST",
    "MQTT_PORT",
    "MQTT_USER",
    "MQTT_PASSWORD",
    "MQTT_DISCOVERY",
    "MQTT_DISCOVERY_PREFIX",
    "MQTT_TOPIC_PREFIX",
    "ADDON_SLUG",
    "SUPERVISOR_TOKEN",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from "nothing configured" so ambient env can't leak in."""
    for name in _MQTT_ENV:
        monkeypatch.delenv(name, raising=False)


def _none() -> None:
    """Stand-in for "no Supervisor" — standalone Docker."""
    return None


def _no_slug() -> str:
    return ""


def _load(supervisor_lookup=_none, slug_lookup=_no_slug):
    return load_config(supervisor_lookup=supervisor_lookup, slug_lookup=slug_lookup)


class TestResolvePrefix:
    def test_override_wins(self) -> None:
        assert resolve_prefix("Custom Prefix", "plenum_beta") == ("custom_prefix", False)

    def test_slug_is_the_zero_config_default(self) -> None:
        assert resolve_prefix("", "plenum_beta") == ("plenum_beta", False)

    def test_falls_back_when_there_is_no_slug(self) -> None:
        assert resolve_prefix("", "") == (DEFAULT_PREFIX, True)

    def test_unusable_candidates_are_skipped_not_used_empty(self) -> None:
        """A prefix that sanitises away would produce an empty topic segment."""
        assert resolve_prefix("!!!", "") == (DEFAULT_PREFIX, True)
        assert resolve_prefix("!!!", "plenum") == ("plenum", False)

    def test_stable_and_beta_do_not_collide(self) -> None:
        assert resolve_prefix("", "plenum")[0] != resolve_prefix("", "plenum_beta")[0]


class TestLoadConfig:
    def test_unconfigured_with_no_broker_source(self) -> None:
        """No Supervisor, no MQTT_HOST → nothing to connect to. This is the only
        state in which the bridge is unavailable; there is no enable option."""
        config = _load()
        assert config.configured is False
        assert config.host == ""

    def test_manual_broker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MQTT_HOST", "broker.local")
        monkeypatch.setenv("MQTT_PORT", "8883")
        monkeypatch.setenv("MQTT_USER", "plenum")
        monkeypatch.setenv("MQTT_PASSWORD", "secret")
        config = _load()
        assert (config.host, config.port) == ("broker.local", 8883)
        assert (config.username, config.password) == ("plenum", "secret")
        assert config.configured is True

    def test_supervisor_discovery_needs_no_configuration_at_all(self) -> None:
        """The HAOS zero-config path — the whole point of the rework. With the
        Supervisor providing a broker, an untouched add-on config is enough."""
        config = _load(
            supervisor_lookup=lambda: {
                "host": "core-mosquitto",
                "port": 1883,
                "username": "addons",
                "password": "pw",
            }
        )
        assert config.host == "core-mosquitto"
        assert config.username == "addons"
        assert config.configured is True

    def test_explicit_host_beats_discovery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An operator who names a broker means it — never silently override."""
        monkeypatch.setenv("MQTT_HOST", "my.broker")

        def _boom():  # pragma: no cover - must never be called
            raise AssertionError("Supervisor discovery ran despite an explicit host")

        assert _load(supervisor_lookup=_boom).host == "my.broker"

    def test_bad_port_falls_back_rather_than_crashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MQTT_HOST", "broker")
        monkeypatch.setenv("MQTT_PORT", "not-a-port")
        assert _load().port == DEFAULT_PORT

    def test_discovery_defaults_on_and_prefix_sanitised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _load()
        assert config.discovery is True
        assert config.discovery_prefix == DEFAULT_DISCOVERY_PREFIX
        monkeypatch.setenv("MQTT_DISCOVERY", "false")
        monkeypatch.setenv("MQTT_DISCOVERY_PREFIX", "HA Discovery")
        config = _load()
        assert config.discovery is False
        assert config.discovery_prefix == "ha_discovery"


class TestSlugResolution:
    """The prefix path that failed in the field: the beta add-on booted with the
    stable ``plenum`` prefix because ``run.sh``'s bashio call yielded nothing.
    The slug now comes from the Supervisor REST API, through ``slug_lookup``."""

    def test_prefix_comes_from_the_looked_up_slug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The exact field failure, inverted: a beta install must get
        ``plenum_beta``, not the shared default."""
        monkeypatch.setenv("MQTT_HOST", "broker")
        config = _load(slug_lookup=lambda: "plenum_beta")
        assert config.prefix == "plenum_beta"
        assert config.prefix_is_fallback is False

    def test_topic_prefix_override_skips_the_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MQTT_HOST", "broker")
        monkeypatch.setenv("MQTT_TOPIC_PREFIX", "custom")

        def _boom() -> str:  # pragma: no cover - must never be called
            raise AssertionError("slug lookup ran despite an explicit prefix override")

        assert _load(slug_lookup=_boom).prefix == "custom"

    def test_addon_slug_env_skips_the_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MQTT_HOST", "broker")
        monkeypatch.setenv("ADDON_SLUG", "plenum_beta")

        def _boom() -> str:  # pragma: no cover - must never be called
            raise AssertionError("slug lookup ran despite ADDON_SLUG being set")

        assert _load(slug_lookup=_boom).prefix == "plenum_beta"

    def test_no_broker_means_no_slug_lookup(self) -> None:
        """With nothing to connect to there are no topics to name — don't make a
        Supervisor call whose answer would be ignored."""

        def _boom() -> str:  # pragma: no cover - must never be called
            raise AssertionError("slug lookup ran with no broker resolved")

        assert _load(slug_lookup=_boom).configured is False

    def test_failed_lookup_falls_back_with_the_flag_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MQTT_HOST", "broker")
        config = _load(slug_lookup=lambda: "")
        assert config.prefix == DEFAULT_PREFIX
        assert config.prefix_is_fallback is True


class TestSupervisorEndpoints:
    """The real Supervisor probes. Never fatal — any failure means "fall back"."""

    def test_no_token_means_no_supervisor(self) -> None:
        assert _supervisor_mqtt() is None
        assert _supervisor_slug() == ""

    def test_mqtt_service_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        body = json.dumps({"result": "ok", "data": {"host": "core-mosquitto", "port": 1883}})
        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(body))
        assert _supervisor_mqtt() == {"host": "core-mosquitto", "port": 1883}

    def test_slug_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """/addons/self/info is how the beta install learns it is plenum_beta."""
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        body = json.dumps({"result": "ok", "data": {"slug": "plenum_beta", "name": "Plenum"}})
        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(body))
        assert _supervisor_slug() == "plenum_beta"

    def test_authorises_with_the_supervisor_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        seen: dict = {}

        def _capture(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            seen["url"] = req.full_url
            return _FakeResponse(json.dumps({"data": {}}))

        monkeypatch.setattr(urllib.request, "urlopen", _capture)
        _supervisor_slug()
        assert seen["auth"] == "Bearer tok"
        assert seen["url"] == "http://supervisor/addons/self/info"

    @pytest.mark.parametrize(
        "failure",
        [
            urllib.error.URLError("no route"),
            TimeoutError(),
            OSError("connection reset"),
        ],
    )
    def test_transport_failures_fall_back(
        self, monkeypatch: pytest.MonkeyPatch, failure: Exception
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")

        def _raise(req, timeout=None):
            raise failure

        monkeypatch.setattr(urllib.request, "urlopen", _raise)
        assert _supervisor_mqtt() is None
        assert _supervisor_slug() == ""

    def test_unparseable_body_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen("not json"))
        assert _supervisor_mqtt() is None
        assert _supervisor_slug() == ""

    def test_missing_or_wrong_shaped_data_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(json.dumps({"data": None})))
        assert _supervisor_mqtt() is None
        assert _supervisor_slug() == ""


class TestLogResolution:
    def test_reports_when_no_broker_exists(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            log_resolution(_load())
        assert any("no broker resolved" in r.message for r in caplog.records)

    def test_no_warning_with_a_slug_derived_prefix(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("MQTT_HOST", "broker")
        config = _load(slug_lookup=lambda: "plenum_beta")
        with caplog.at_level(logging.WARNING):
            log_resolution(config)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_fallback_warning_names_standalone_docker_without_a_supervisor(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("MQTT_HOST", "broker")
        config = _load()
        with caplog.at_level(logging.WARNING):
            log_resolution(config)
        messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("standalone Docker" in m for m in messages)

    def test_fallback_warning_blames_the_lookup_when_a_supervisor_is_present(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The field failure's log line said "standalone Docker" on real HAOS,
        which sent diagnosis the wrong way. With a Supervisor present the
        warning must say what actually happened: the slug lookup failed."""
        monkeypatch.setenv("MQTT_HOST", "broker")
        config = _load()
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        with caplog.at_level(logging.WARNING):
            log_resolution(config)
        messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("slug could not be resolved" in m for m in messages)
        assert not any("standalone Docker" in m for m in messages)
