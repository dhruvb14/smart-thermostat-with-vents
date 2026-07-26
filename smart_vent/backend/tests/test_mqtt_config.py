"""Broker + topic-prefix resolution (Issue #519)."""

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
    "MQTT_ENABLED",
    "MQTT_HOST",
    "MQTT_PORT",
    "MQTT_USER",
    "MQTT_PASSWORD",
    "MQTT_DISCOVERY",
    "MQTT_DISCOVERY_PREFIX",
    "MQTT_TOPIC_PREFIX",
    "ADDON_SLUG",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from "nothing configured" so ambient env can't leak in."""
    for name in _MQTT_ENV:
        monkeypatch.delenv(name, raising=False)


def _none() -> None:
    """Stand-in for "no Supervisor" — standalone Docker."""
    return None


class TestResolvePrefix:
    def test_override_wins(self) -> None:
        assert resolve_prefix("Custom Prefix", "plenum_beta") == ("custom_prefix", False)

    def test_slug_is_the_zero_config_default(self) -> None:
        assert resolve_prefix("", "plenum_beta") == ("plenum_beta", False)

    def test_falls_back_when_there_is_no_slug(self) -> None:
        """Standalone Docker: no Supervisor means no slug exists at all."""
        assert resolve_prefix("", "") == (DEFAULT_PREFIX, True)

    def test_unusable_candidates_are_skipped_not_used_empty(self) -> None:
        """A prefix that sanitises away would produce an empty topic segment."""
        assert resolve_prefix("!!!", "") == (DEFAULT_PREFIX, True)
        assert resolve_prefix("!!!", "plenum") == ("plenum", False)

    def test_stable_and_beta_do_not_collide(self) -> None:
        assert resolve_prefix("", "plenum")[0] != resolve_prefix("", "plenum_beta")[0]


class TestLoadConfig:
    def test_disabled_by_default(self) -> None:
        config = load_config(supervisor_lookup=_none)
        assert config.enabled is False
        assert config.configured is False

    def test_manual_broker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MQTT_ENABLED", "true")
        monkeypatch.setenv("MQTT_HOST", "broker.local")
        monkeypatch.setenv("MQTT_PORT", "8883")
        monkeypatch.setenv("MQTT_USER", "plenum")
        monkeypatch.setenv("MQTT_PASSWORD", "secret")
        config = load_config(supervisor_lookup=_none)
        assert (config.host, config.port) == ("broker.local", 8883)
        assert (config.username, config.password) == ("plenum", "secret")
        assert config.configured is True

    def test_supervisor_discovery_fills_the_blanks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MQTT_ENABLED", "true")
        config = load_config(
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
        monkeypatch.setenv("MQTT_ENABLED", "true")
        monkeypatch.setenv("MQTT_HOST", "my.broker")

        def _boom():  # pragma: no cover - must never be called
            raise AssertionError("Supervisor discovery ran despite an explicit host")

        assert load_config(supervisor_lookup=_boom).host == "my.broker"

    def test_discovery_is_not_attempted_while_disabled(self) -> None:
        def _boom():  # pragma: no cover - must never be called
            raise AssertionError("Supervisor was contacted while MQTT is off")

        assert load_config(supervisor_lookup=_boom).configured is False

    def test_bad_port_falls_back_rather_than_crashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MQTT_ENABLED", "true")
        monkeypatch.setenv("MQTT_HOST", "broker")
        monkeypatch.setenv("MQTT_PORT", "not-a-port")
        assert load_config(supervisor_lookup=_none).port == DEFAULT_PORT

    def test_discovery_defaults_on_and_prefix_sanitised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = load_config(supervisor_lookup=_none)
        assert config.discovery is True
        assert config.discovery_prefix == DEFAULT_DISCOVERY_PREFIX
        monkeypatch.setenv("MQTT_DISCOVERY", "false")
        monkeypatch.setenv("MQTT_DISCOVERY_PREFIX", "HA Discovery")
        config = load_config(supervisor_lookup=_none)
        assert config.discovery is False
        assert config.discovery_prefix == "ha_discovery"

    def test_enabled_but_unreachable_broker_is_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Enabled with nowhere to connect must not read as "ready"."""
        monkeypatch.setenv("MQTT_ENABLED", "true")
        assert load_config(supervisor_lookup=_none).configured is False


class TestSupervisorLookup:
    """The real Supervisor probe. Never fatal — a missing or broken MQTT
    service just means "fall back to the configured broker"."""

    def test_no_token_means_no_supervisor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        assert _supervisor_mqtt() is None

    def test_reads_the_service_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        body = json.dumps({"result": "ok", "data": {"host": "core-mosquitto", "port": 1883}})
        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(body))
        assert _supervisor_mqtt() == {"host": "core-mosquitto", "port": 1883}

    def test_authorises_with_the_supervisor_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        seen: dict = {}

        def _capture(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            return _FakeResponse(json.dumps({"data": {}}))

        monkeypatch.setattr(urllib.request, "urlopen", _capture)
        _supervisor_mqtt()
        assert seen["auth"] == "Bearer tok"

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

    def test_unparseable_body_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen("not json"))
        assert _supervisor_mqtt() is None

    def test_missing_or_wrong_shaped_data_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(json.dumps({"data": None})))
        assert _supervisor_mqtt() is None


class TestLogResolution:
    def test_warns_when_the_prefix_could_collide(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("MQTT_ENABLED", "true")
        monkeypatch.setenv("MQTT_HOST", "broker")
        config = load_config(supervisor_lookup=_none)
        with caplog.at_level(logging.WARNING):
            log_resolution(config)
        assert any("collide" in r.message for r in caplog.records)

    def test_no_warning_with_a_slug_derived_prefix(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("MQTT_ENABLED", "true")
        monkeypatch.setenv("MQTT_HOST", "broker")
        monkeypatch.setenv("ADDON_SLUG", "plenum_beta")
        config = load_config(supervisor_lookup=_none)
        with caplog.at_level(logging.WARNING):
            log_resolution(config)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_warns_when_enabled_with_no_broker(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("MQTT_ENABLED", "true")
        with caplog.at_level(logging.WARNING):
            log_resolution(load_config(supervisor_lookup=_none))
        assert any("no broker" in r.message for r in caplog.records)

    def test_silent_when_disabled(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            log_resolution(load_config(supervisor_lookup=_none))
        assert any("disabled" in r.message for r in caplog.records)
