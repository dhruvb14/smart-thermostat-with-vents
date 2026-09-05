"""Parity between ``HAClient`` and its integration double (Issue #608).

``FakeHomeAssistant``'s module docstring promises it "mirrors every public
method on ``HAClient``". Nothing enforced that promise, so the next public
method added to the real client could be missed silently — and, worse, a
route could keep reaching past the public surface into a private member
without anything noticing. ``GET /api/ha/entities`` did exactly that
(``ha._state_cache``), and the coverage campaign papered over it by
assigning ``_state_cache`` onto the double inside the test.

Two rules, in the spirit of ``test_temperature_field_parity.py`` and
``test_mqtt_registry.py``:

1. Production code outside ``ha_client.py`` talks to ``HAClient`` only
   through its public API, so the double can be a faithful stand-in without
   knowing the real client's internal field names.
2. Every public callable and data attribute on ``HAClient`` exists on
   ``FakeHomeAssistant``, with a matching signature.

``request.app["ha"]`` is typed ``Any`` by aiohttp, so mypy cannot see a
private reach; these tests are the only thing that can.
"""

from __future__ import annotations

import inspect
import pathlib
import re

import pytest

from backend import ha_client as ha_client_mod
from backend.ha_client import HAClient
from backend.tests.integration.fake_ha import FakeHomeAssistant

# Members the fake is NOT required to mirror. This is a decision record, not
# a denylist to pad: each entry must be something a double has no business
# owning. Adding to it is a deliberate choice that needs a reason here.
EXEMPT: frozenset[str] = frozenset(
    {
        # WebSocket/session plumbing. The fake is backed by a dict; it has no
        # wire, no reconnect loop and no aiohttp session to manage.
        "start",
        "stop",
        # Connection lifecycle helpers that only make sense against a socket.
        # `wait_connected` IS mirrored (engine code awaits it) and so is not
        # listed here.
    }
)


def _public_callables(cls: type) -> dict[str, inspect.Signature]:
    """Public methods declared on ``cls`` (not inherited from ``object``)."""
    out: dict[str, inspect.Signature] = {}
    for name, member in inspect.getmembers(cls, callable):
        if name.startswith("_"):
            continue
        if getattr(object, name, None) is member:
            continue
        out[name] = inspect.signature(member)
    return out


class TestPublicSurfaceParity:
    def test_every_public_haclient_method_exists_on_the_fake(self):
        real = _public_callables(HAClient)
        fake = _public_callables(FakeHomeAssistant)
        missing = sorted(set(real) - set(fake) - EXEMPT)
        assert not missing, (
            f"FakeHomeAssistant is missing public HAClient method(s): {missing}. "
            "Add them to the fake, or name them in EXEMPT with a reason."
        )

    def test_shared_methods_have_matching_signatures(self):
        real = _public_callables(HAClient)
        fake = _public_callables(FakeHomeAssistant)
        diverged = {
            name: (str(real[name]), str(fake[name]))
            for name in sorted(set(real) & set(fake))
            if real[name] != fake[name]
        }
        assert not diverged, (
            "Signature drift between HAClient and FakeHomeAssistant — a test "
            f"passing against the fake would not prove the real call works: {diverged}"
        )

    def test_public_data_attributes_exist_on_the_fake(self):
        """Instance attributes set in ``__init__`` (e.g. ``ha_temp_unit``).

        These never appear on the CLASS, so a class-level ``hasattr`` check
        would pass vacuously. Instantiate both and compare the instance
        dictionaries' public keys instead.
        """
        real = HAClient(ha_url="http://ha.invalid", token="t")
        fake = FakeHomeAssistant()
        real_attrs = {k for k in vars(real) if not k.startswith("_")}
        fake_attrs = {k for k in vars(fake) if not k.startswith("_")}
        missing = sorted(real_attrs - fake_attrs)
        assert not missing, f"FakeHomeAssistant is missing public HAClient attribute(s): {missing}"


class TestNoPrivateReachFromProduction:
    """No production module outside ``ha_client.py`` touches ``HAClient``'s
    privates. ``GET /api/ha/entities`` used to read ``ha._state_cache``
    directly; ``HAClient.all_states()`` replaced it (#608)."""

    # `ha` is how routes/engine name the client; `_ha` is the scheduler's
    # attribute for it. Either followed by a private member is the smell.
    PRIVATE_REACH = re.compile(r"\bha\._[A-Za-z_]|\bself\._ha\._[A-Za-z_]")

    def test_no_module_reaches_into_haclient_privates(self):
        backend = pathlib.Path(ha_client_mod.__file__).parent
        offenders: list[str] = []
        for path in sorted(backend.rglob("*.py")):
            rel = path.relative_to(backend)
            # The client may touch its own privates; tests are not production.
            if rel.parts[0] == "tests" or rel.name == "ha_client.py":
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if self.PRIVATE_REACH.search(line):
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")
        assert not offenders, (
            "Production code must reach HAClient only through its public API "
            "(add an accessor like all_states() instead):\n" + "\n".join(offenders)
        )


class TestAllStatesAccessor:
    @pytest.mark.asyncio
    async def test_returns_every_cached_state_and_is_a_copy(self):
        fake = FakeHomeAssistant()
        fake.seed_state("sensor.a", "1", {"friendly_name": "A"})
        fake.seed_state("climate.b", "cool", {})

        states = fake.all_states()
        assert {s["entity_id"] for s in states} == {"sensor.a", "climate.b"}

        # Mutating the returned list must not disturb the cache.
        states.clear()
        assert len(fake.all_states()) == 2

    def test_real_client_all_states_reads_the_cache(self):
        real = HAClient(ha_url="http://ha.invalid", token="t")
        assert real.all_states() == []
        real._state_cache["sensor.x"] = {"entity_id": "sensor.x", "state": "7"}
        assert real.all_states() == [{"entity_id": "sensor.x", "state": "7"}]

    def test_all_states_does_not_wait_for_the_connection(self):
        """The accessor is deliberately sync and non-waiting, unlike its
        domain-filtered sibling ``get_entities_by_domain`` (#608). Making it
        wait would silently turn ``GET /api/ha/entities`` from "return what is
        cached" into "block until HA connects" — a behaviour change the
        accessor exists to avoid, not to smuggle in."""
        real = HAClient(ha_url="http://ha.invalid", token="t")
        assert not real.is_connected  # never connected
        real._state_cache["sensor.x"] = {"entity_id": "sensor.x", "state": "7"}
        # Returns immediately with the cached row rather than blocking.
        assert real.all_states() == [{"entity_id": "sensor.x", "state": "7"}]


class TestConnectionAndDevLoggerAccessors:
    """The two accessors added so ``scheduler.py`` stops touching
    ``HAClient._connected`` and ``._dev_logger`` directly (#608)."""

    def test_is_connected_tracks_the_underlying_event_both_ways(self):
        real = HAClient(ha_url="http://ha.invalid", token="t")
        assert real.is_connected is False
        real._connected.set()
        assert real.is_connected is True
        real._connected.clear()
        assert real.is_connected is False

    def test_set_dev_logger_round_trips_and_accepts_none(self):
        real = HAClient(ha_url="http://ha.invalid", token="t")
        assert real._dev_logger is None
        sentinel = object()
        real.set_dev_logger(sentinel)
        assert real._dev_logger is sentinel
        # Clearing it must work too — dev mode can be turned back off.
        real.set_dev_logger(None)
        assert real._dev_logger is None

    def test_fake_accessors_behave_identically(self):
        fake = FakeHomeAssistant()
        # The fake starts "connected" so integration tests need no handshake.
        assert fake.is_connected is True
        fake._connected.clear()
        assert fake.is_connected is False

        sentinel = object()
        fake.set_dev_logger(sentinel)
        assert fake._dev_logger is sentinel
