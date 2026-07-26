"""Sanitized room-name uniqueness: write boundary + upgrade migration (#519).

MQTT addresses rooms by sanitised name as well as by GUID, and sanitising is
lossy, so uniqueness has to hold on the sanitised form. The rule is
unconditional — it does not depend on MQTT being enabled — because a name that
turns ambiguous the moment MQTT is switched on is a latent bug.
"""

from __future__ import annotations

import aiosqlite
import pytest

from backend import db as _db

THERMO = "climate.test_thermostat"


async def _create(client, name: str):
    return await client.post("/api/rooms", json={"name": name, "thermostat_entity_id": THERMO})


class TestWriteBoundary:
    @pytest.mark.asyncio
    async def test_distinct_names_are_fine(self, client, fake_ha) -> None:
        assert (await _create(client, "Office")).status == 201
        assert (await _create(client, "Kitchen")).status == 201

    @pytest.mark.asyncio
    async def test_exact_duplicate_is_rejected(self, client, fake_ha) -> None:
        assert (await _create(client, "Office")).status == 201
        resp = await _create(client, "Office")
        assert resp.status == 400
        assert "already called" in (await resp.json())["error"]

    @pytest.mark.parametrize("variant", ["office", "OFFICE", "  Office  ", "Office!", "-Office-"])
    @pytest.mark.asyncio
    async def test_case_and_punctuation_variants_collide(
        self, client, fake_ha, variant: str
    ) -> None:
        """Stricter than raw-string uniqueness, and deliberately so: these all
        sanitise to the same topic segment."""
        await _create(client, "Office")
        assert (await _create(client, variant)).status == 400

    @pytest.mark.parametrize("variant", ["Off Ice", "off-ice", "Office 2"])
    @pytest.mark.asyncio
    async def test_names_that_stay_distinct_are_allowed(
        self, client, fake_ha, variant: str
    ) -> None:
        """The rule must not over-reach: a space and a hyphen survive
        sanitisation, so these are genuinely different topic segments."""
        await _create(client, "Office")
        assert (await _create(client, variant)).status == 201

    @pytest.mark.asyncio
    async def test_names_that_sanitise_to_nothing_are_rejected(self, client, fake_ha) -> None:
        resp = await _create(client, "!!!")
        assert resp.status == 400
        assert "letter or number" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_rename_onto_another_room_is_rejected(self, client, fake_ha) -> None:
        await _create(client, "Office")
        room_id = (await (await _create(client, "Kitchen")).json())["id"]

        resp = await client.put(f"/api/rooms/{room_id}", json={"name": "office"})
        assert resp.status == 400
        # The rejected rename must leave the room completely untouched.
        assert (await (await client.get(f"/api/rooms/{room_id}")).json())["name"] == "Kitchen"

    @pytest.mark.asyncio
    async def test_saving_a_room_under_its_own_name_is_not_a_conflict(
        self, client, fake_ha
    ) -> None:
        """The form re-sends the current name on every save."""
        room_id = (await (await _create(client, "Office")).json())["id"]
        resp = await client.put(f"/api/rooms/{room_id}", json={"name": "Office", "temp_offset": 1})
        assert resp.status == 200, await resp.text()

    @pytest.mark.asyncio
    async def test_a_room_can_be_renamed_to_a_free_name(self, client, fake_ha) -> None:
        room_id = (await (await _create(client, "Office")).json())["id"]
        resp = await client.put(f"/api/rooms/{room_id}", json={"name": "Study"})
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["name"] == "Study"

    @pytest.mark.asyncio
    async def test_a_deleted_room_frees_its_name(self, client, fake_ha) -> None:
        room_id = (await (await _create(client, "Office")).json())["id"]
        await client.delete(f"/api/rooms/{room_id}")
        assert (await _create(client, "Office")).status == 201

    @pytest.mark.asyncio
    async def test_updates_that_do_not_touch_the_name_are_unaffected(self, client, fake_ha) -> None:
        room_id = (await (await _create(client, "Office")).json())["id"]
        resp = await client.put(f"/api/rooms/{room_id}", json={"temp_offset": 2})
        assert resp.status == 200, await resp.text()


class TestUpgradeMigration:
    """Installs predating the invariant can already hold collisions; they are
    repaired automatically rather than blocking startup or staying ambiguous."""

    async def _rooms_with_names(self, path: str, names: list[str]) -> None:
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        try:
            await _db.init_db(conn)
            # Insert past the API on purpose — that is the pre-#519 state.
            for index, name in enumerate(names):
                await conn.execute(
                    "INSERT INTO rooms(id,name,thermostat_entity_id) VALUES(?,?,?)",
                    (f"room-{index}", name, THERMO),
                )
            await conn.commit()
        finally:
            await conn.close()

    async def _run_migration(self, path: str):
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        try:
            renames = await _db._migrate_room_name_uniqueness(conn)
            async with conn.execute("SELECT id, name FROM rooms ORDER BY rowid") as cur:
                names = [str(r["name"]) for r in await cur.fetchall()]
            return renames, names
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_no_collisions_changes_nothing(self, db_path: str) -> None:
        await self._rooms_with_names(db_path, ["Office", "Kitchen"])
        renames, names = await self._run_migration(db_path)
        assert renames == []
        assert names == ["Office", "Kitchen"]

    @pytest.mark.asyncio
    async def test_collision_is_repaired_keeping_the_first(self, db_path: str) -> None:
        await self._rooms_with_names(db_path, ["Office", "office"])
        renames, names = await self._run_migration(db_path)
        # The renamed room keeps its own capitalisation — the suffix is the
        # only change, so the user still recognises the room they named.
        assert names == ["Office", "office (2)"]
        assert renames == [("office", "office (2)")]

    @pytest.mark.asyncio
    async def test_three_way_collision(self, db_path: str) -> None:
        await self._rooms_with_names(db_path, ["Office", "office", "OFFICE"])
        _, names = await self._run_migration(db_path)
        assert names == ["Office", "office (2)", "OFFICE (3)"]

    @pytest.mark.asyncio
    async def test_unaddressable_name_is_repaired_too(self, db_path: str) -> None:
        """An all-punctuation name cannot be a topic segment at all, collision
        or not."""
        await self._rooms_with_names(db_path, ["!!!"])
        renames, names = await self._run_migration(db_path)
        assert renames and names[0].startswith("Room ")

    @pytest.mark.asyncio
    async def test_rerunning_is_a_no_op(self, db_path: str) -> None:
        """It runs on every boot, so a second pass must not keep renaming."""
        await self._rooms_with_names(db_path, ["Office", "office"])
        await self._run_migration(db_path)
        renames, names = await self._run_migration(db_path)
        assert renames == []
        assert names == ["Office", "office (2)"]

    @pytest.mark.asyncio
    async def test_result_satisfies_the_write_boundary_rule(self, db_path: str) -> None:
        """Whatever the migration produces must itself be a legal name set."""
        from backend.mqtt.naming import sanitize

        await self._rooms_with_names(db_path, ["Office", "office", "OFFICE", "!!!", "@@@"])
        _, names = await self._run_migration(db_path)
        keys = [sanitize(n) for n in names]
        assert all(keys) and len(keys) == len(set(keys))

    @pytest.mark.asyncio
    async def test_renames_are_surfaced_in_the_event_log(self, db_path: str) -> None:
        """#519: automatic, but never silent."""
        await self._rooms_with_names(db_path, ["Office", "office"])

        from backend.event_logger import EventLogger
        from backend.ha_client import HAClient  # noqa: F401 - typing only
        from backend.scheduler import Scheduler

        from .fake_ha import FakeHomeAssistant

        logger = EventLogger()
        scheduler = Scheduler(
            ha=FakeHomeAssistant(),  # type: ignore[arg-type]
            db_path=db_path,
            event_logger=logger,
        )
        await scheduler.start()
        try:
            conn = scheduler._db_conn
            async with conn.execute(
                "SELECT message FROM event_log WHERE message LIKE 'Renamed room%'"
            ) as cur:
                rows = [str(r["message"]) for r in await cur.fetchall()]
        finally:
            await scheduler.stop()

        assert rows and "office (2)" in rows[0]
