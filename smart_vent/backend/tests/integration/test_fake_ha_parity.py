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
2. Every public member of ``HAClient`` exists on ``FakeHomeAssistant``:
   callables (with a matching signature), ``property``/data descriptors, and
   instance data attributes. All three shapes are checked, because they are
   found three different ways — ``inspect.getmembers(cls, callable)`` cannot
   see a ``property`` (a property object is not callable) and ``vars(instance)``
   cannot see a class-level descriptor, so a guard built on either one alone
   is blind to exactly the shape ``HAClient.is_connected`` has.

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

# Members the fake is NOT required to mirror. Deliberately EMPTY: the fake
# currently mirrors HAClient's entire public surface, including `start`/`stop`
# — it models a connection lifecycle (`fake_ha.py`) even though it is backed by
# a dict, because engine and scheduler code drives those methods. This stays as
# the escape hatch, and it is a decision record rather than a denylist to pad:
# an entry must be something a double has no business owning, and adding one
# needs its reason written here. Note an exemption only excuses a MISSING
# member — a name present on both classes is still signature-compared.
EXEMPT: frozenset[str] = frozenset()


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


def _public_descriptors(cls: type) -> set[str]:
    """Public ``property`` (and other data-descriptor) names on ``cls``.

    Neither sibling helper can see these. ``inspect.getmembers(cls, callable)``
    filters them out because a ``property`` object is not callable, and
    ``vars(instance)`` never contains a class-level descriptor. So without this
    the parity guard would be blind to precisely the shape #608 introduced —
    ``HAClient.is_connected`` — and someone adding, say, ``@property def
    outside_temp_entity`` would leave the double behind with all the other
    parity tests still green.
    """
    return {
        name
        for klass in cls.__mro__
        for name, member in vars(klass).items()
        if not name.startswith("_") and inspect.isdatadescriptor(member)
    }


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

    def test_every_public_haclient_property_exists_on_the_fake(self):
        """The third shape: ``@property``.

        ``is_connected`` (#608) is one, and the two callable-based tests above
        cannot see it — a ``property`` object fails ``callable()``. Without this
        test, adding a property to ``HAClient`` and forgetting the fake leaves
        every parity test green while every integration request through the
        route that reads it 500s on ``AttributeError``.
        """
        real = _public_descriptors(HAClient)
        fake = _public_descriptors(FakeHomeAssistant)
        missing = sorted(real - fake - EXEMPT)
        assert not missing, (
            f"FakeHomeAssistant is missing public HAClient property/properties: {missing}. "
            "Add them to the fake, or name them in EXEMPT with a reason."
        )
        # A property on the real client must not be a plain method on the fake:
        # `ha.is_connected` would then return a bound method (always truthy)
        # instead of a bool, and the double would silently disagree.
        assert not sorted(real & _public_callables(FakeHomeAssistant).keys()), (
            "FakeHomeAssistant implements an HAClient property as a callable; "
            "it must be a property there too."
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

    # Grounded in reflection rather than in how a caller happens to spell the
    # variable. A pattern keyed on the receiver (`ha._x`, `self._ha._x`) misses
    # the same reach written any other way — `request.app["ha"]._state_cache`,
    # or a client bound to a differently-named local — so #608's own bug would
    # have escaped it had it been written as a one-liner. Instead: take
    # HAClient's ACTUAL private member names and look for any of them accessed
    # off something other than `self`.
    #
    # The `(?<!self)` lookbehind is what keeps this quiet. A module reaching
    # into the HA client always goes through some *other* object, whereas a
    # module touching its own same-named private (`mqtt/bridge.py` owns a
    # `_dispatch` and a `_read_loop`) writes `self.`. `ha_client.py` itself is
    # skipped below, so nothing legitimate is left.
    _HA_PRIVATES = sorted(
        (
            {n for n in vars(HAClient) if n.startswith("_") and not n.startswith("__")}
            | {
                n
                for n in vars(HAClient(ha_url="http://ha.invalid", token="t"))
                if n.startswith("_") and not n.startswith("__")
            }
        ),
        key=lambda n: (-len(n), n),  # longest first so `_connected` beats `_connect`
    )
    PRIVATE_REACH = re.compile(r"(?<!self)\.(?:" + "|".join(_HA_PRIVATES) + r")\b")

    def test_the_guard_matches_every_spelling_of_the_reach(self):
        """The regex is the whole test, so pin what it does and does not catch.

        The first line is #608's actual bug; the rest are the spellings a
        receiver-keyed pattern would have missed. The last two are the
        false-positive shapes that must stay quiet — a module using its own
        identically-named private via ``self``.
        """
        for reach in (
            "entities = list(ha._state_cache.values())",
            'entities = list(request.app["ha"]._state_cache.values())',
            "if not self._ha._connected.is_set():",
            "self._ha_client.set_dev_logger(x) or client._dev_logger",
            "some_other_name._wildcard_listeners.clear()",
        ):
            assert self.PRIVATE_REACH.search(reach), reach
        for ok in (
            "reader = asyncio.create_task(self._read_loop(transport))",
            "status, payload = await self._dispatch('GET', path, None)",
        ):
            assert not self.PRIVATE_REACH.search(ok), ok

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
