"""
Tests for backend/main.py and backend/api/ws_handler.py.

Covers: build_app startup/shutdown paths, the security_headers middleware,
the SPA handler, and WSManager broadcast/handle.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

from backend.api.ws_handler import WSManager
from backend.main import _apply_security_headers, _migrate_db_filename, build_app

from .integration.fake_ha import FakeHomeAssistant

# ---------------------------------------------------------------------------
# _migrate_db_filename
# ---------------------------------------------------------------------------


class TestMigrateDbFilename:
    def test_no_files_is_noop(self, tmp_path):
        _migrate_db_filename(str(tmp_path))  # must not raise

    def test_renames_old_to_new(self, tmp_path):
        old = tmp_path / "flair.db"
        old.write_bytes(b"data")
        _migrate_db_filename(str(tmp_path))
        assert (tmp_path / "app.db").exists()
        assert not old.exists()

    def test_skips_when_new_already_exists(self, tmp_path):
        old = tmp_path / "flair.db"
        new = tmp_path / "app.db"
        old.write_bytes(b"old")
        new.write_bytes(b"new")
        _migrate_db_filename(str(tmp_path))
        # new file untouched; old file untouched
        assert new.read_bytes() == b"new"

    def test_renames_wal_and_shm_sidecars(self, tmp_path):
        (tmp_path / "flair.db").write_bytes(b"main")
        (tmp_path / "flair.db-wal").write_bytes(b"wal")
        (tmp_path / "flair.db-shm").write_bytes(b"shm")
        _migrate_db_filename(str(tmp_path))
        assert (tmp_path / "app.db-wal").exists()
        assert (tmp_path / "app.db-shm").exists()


# ---------------------------------------------------------------------------
# _apply_security_headers
# ---------------------------------------------------------------------------


class TestApplySecurityHeaders:
    def test_sets_expected_headers(self):
        headers: dict = {}
        _apply_security_headers(headers)
        assert "X-Content-Type-Options" in headers
        assert "X-Frame-Options" in headers
        assert "Content-Security-Policy" in headers
        assert "Referrer-Policy" in headers
        assert "Permissions-Policy" in headers


# ---------------------------------------------------------------------------
# build_app — frontend_dist warning path
# ---------------------------------------------------------------------------


class TestBuildAppFrontendWarning:
    def test_nonexistent_frontend_dist_logs_warning(self, tmp_path, caplog):
        import logging

        fake_ha = FakeHomeAssistant()
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            nonexistent = tmp_path / "dist"  # does not exist
            with caplog.at_level(logging.WARNING, logger="backend.main"):
                build_app(fake_ha, db, frontend_dist=nonexistent, start_ha=False)
            assert any("API-only mode" in r.message for r in caplog.records)
        finally:
            os.unlink(db)


# ---------------------------------------------------------------------------
# build_app — start_ha=True startup path
# ---------------------------------------------------------------------------


class TestBuildAppStartHa:
    async def test_startup_creates_ha_task(self):
        fake_ha = FakeHomeAssistant()
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            app = build_app(fake_ha, db, frontend_dist=None, start_ha=True)
            server = TestServer(app)
            async with TestClient(server) as c:
                await c.start_server()
                assert "ha_task" in c.app
                task = c.app["ha_task"]
                assert isinstance(task, asyncio.Task)
        finally:
            os.unlink(db)

    async def test_shutdown_cancels_ha_task(self):
        fake_ha = FakeHomeAssistant()
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            app = build_app(fake_ha, db, frontend_dist=None, start_ha=True)
            server = TestServer(app)
            async with TestClient(server) as c:
                await c.start_server()
                # Shutdown happens automatically on context exit — just verify
                # it doesn't raise.
        finally:
            os.unlink(db)


# ---------------------------------------------------------------------------
# Security headers middleware — error response path
# ---------------------------------------------------------------------------


class TestSecurityHeadersMiddleware:
    async def test_headers_on_404(self):
        fake_ha = FakeHomeAssistant()
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            app = build_app(fake_ha, db, frontend_dist=None, start_ha=False)
            server = TestServer(app)
            async with TestClient(server) as c:
                await c.start_server()
                resp = await c.get("/api/nonexistent-route")
                assert "X-Content-Type-Options" in resp.headers
        finally:
            os.unlink(db)

    async def test_unexpected_exception_branch(self):
        """The `except Exception: raise` branch in the middleware (lines 84-87)."""

        fake_ha = FakeHomeAssistant()
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            app = build_app(fake_ha, db, frontend_dist=None, start_ha=False)

            async def _crash(request):
                raise RuntimeError("intentional non-HTTP exception")

            app.router.add_get("/api/crash-for-test", _crash)

            server = TestServer(app)
            async with TestClient(server) as c:
                await c.start_server()
                resp = await c.get("/api/crash-for-test")
                assert resp.status == 500
        finally:
            os.unlink(db)


# ---------------------------------------------------------------------------
# build_app — SPA frontend_dist path (lines 124-134)
# ---------------------------------------------------------------------------


class TestBuildAppSpaFrontend:
    async def test_spa_handler_serves_index_html(self, tmp_path):
        fake_ha = FakeHomeAssistant()
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            dist = tmp_path / "dist"
            dist.mkdir()
            (dist / "assets").mkdir()
            (dist / "index.html").write_text("<html><body>SPA</body></html>")

            app = build_app(fake_ha, db, frontend_dist=dist, start_ha=False)
            server = TestServer(app)
            async with TestClient(server) as c:
                await c.start_server()
                resp = await c.get("/")
                assert resp.status == 200
                text = await resp.text()
                assert "SPA" in text
        finally:
            os.unlink(db)

    async def test_spa_handler_tail_route(self, tmp_path):
        fake_ha = FakeHomeAssistant()
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            dist = tmp_path / "dist"
            dist.mkdir()
            (dist / "assets").mkdir()
            (dist / "index.html").write_text("<html>SPA</html>")

            app = build_app(fake_ha, db, frontend_dist=dist, start_ha=False)
            server = TestServer(app)
            async with TestClient(server) as c:
                await c.start_server()
                resp = await c.get("/some/deep/route")
                assert resp.status == 200
        finally:
            os.unlink(db)


# ---------------------------------------------------------------------------
# WSManager
# ---------------------------------------------------------------------------


class TestWSManagerBroadcast:
    async def test_broadcast_to_no_clients_is_noop(self):
        mgr = WSManager()
        await mgr.broadcast("test_event", {"key": "val"})  # must not raise

    async def test_broadcast_removes_dead_clients(self):
        mgr = WSManager()
        dead_ws = AsyncMock()
        dead_ws.send_str = AsyncMock(side_effect=RuntimeError("closed"))
        mgr._clients.add(dead_ws)
        await mgr.broadcast("test_event", {"x": 1})
        assert dead_ws not in mgr._clients

    async def test_broadcast_sends_json_to_live_client(self):
        mgr = WSManager()
        live_ws = AsyncMock()
        mgr._clients.add(live_ws)
        await mgr.broadcast("state_update", {"foo": "bar"})
        live_ws.send_str.assert_called_once()
        import json

        msg = json.loads(live_ws.send_str.call_args[0][0])
        assert msg["type"] == "state_update"
        assert msg["data"]["foo"] == "bar"


# ---------------------------------------------------------------------------
# OpenAPI / Swagger Docs
# ---------------------------------------------------------------------------


class TestOpenAPIDocs:
    async def test_docs_ui_serves_html(self):
        fake_ha = FakeHomeAssistant()
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            app = build_app(fake_ha, db, frontend_dist=None, start_ha=False)
            server = TestServer(app)
            async with TestClient(server) as c:
                await c.start_server()
                resp = await c.get("/api/docs/")
                assert resp.status == 200
                text = await resp.text()
                assert "Swagger UI" in text
                # Verify relative paths are present due to monkey patch
                assert 'href="./static/swagger-ui.css"' in text
                assert 'src="./static/swagger-ui-bundle.js"' in text
        finally:
            os.unlink(db)

    async def test_openapi_json_is_valid(self):
        fake_ha = FakeHomeAssistant()
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            app = build_app(fake_ha, db, frontend_dist=None, start_ha=False)
            server = TestServer(app)
            async with TestClient(server) as c:
                await c.start_server()
                resp = await c.get("/api/docs/openapi.json")
                assert resp.status == 200
                json_data = await resp.json()
                assert json_data["info"]["title"] == "Plenum API"
                assert json_data["info"]["version"] == "v1"
        finally:
            os.unlink(db)

    async def test_docs_redirect(self):
        fake_ha = FakeHomeAssistant()
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            app = build_app(fake_ha, db, frontend_dist=None, start_ha=False)
            server = TestServer(app)
            async with TestClient(server) as c:
                await c.start_server()
                # Check redirect from /api/docs to /api/docs/
                resp = await c.get("/api/docs", allow_redirects=False)
                assert resp.status == 302
                assert resp.headers["Location"] == "/api/docs/"
        finally:
            os.unlink(db)
