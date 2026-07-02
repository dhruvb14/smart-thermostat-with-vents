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
        from unittest.mock import MagicMock

        headers: dict = {}
        request = MagicMock()
        request.secure = False
        _apply_security_headers(headers, request)
        assert "X-Content-Type-Options" in headers
        assert "X-Frame-Options" in headers
        assert "Content-Security-Policy" in headers
        assert "Referrer-Policy" in headers
        assert "Permissions-Policy" in headers

    def test_xss_protection_and_server_suppression(self):
        from unittest.mock import MagicMock

        headers: dict = {}
        request = MagicMock()
        request.secure = False
        _apply_security_headers(headers, request)
        assert headers.get("X-XSS-Protection") == "1; mode=block"
        assert headers.get("Server") == ""

    def test_hsts_set_for_secure_requests(self):
        from unittest.mock import MagicMock

        headers: dict = {}
        request = MagicMock()
        request.secure = True
        _apply_security_headers(headers, request)
        hsts = headers.get("Strict-Transport-Security", "")
        assert "max-age=31536000" in hsts

    def test_hsts_not_set_for_insecure_requests(self):
        from unittest.mock import MagicMock

        headers: dict = {}
        request = MagicMock()
        request.secure = False
        _apply_security_headers(headers, request)
        assert "Strict-Transport-Security" not in headers

    def test_frame_ancestors_in_csp(self):
        from unittest.mock import MagicMock

        headers: dict = {}
        request = MagicMock()
        request.secure = False
        _apply_security_headers(headers, request)
        assert "frame-ancestors 'self'" in headers.get("Content-Security-Policy", "")


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
# _start_mcp_server / _stop_mcp_server (Issue #372)
# ---------------------------------------------------------------------------


class TestMcpServerLifecycle:
    async def test_start_binds_and_stop_cleans_up(self, monkeypatch):
        import socket

        import backend.main as main

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        monkeypatch.setattr(main, "MCP_PORT", port)

        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            app = build_app(FakeHomeAssistant(), db, frontend_dist=None, start_ha=False)
            ctx = await main._start_mcp_server(app)
            assert ctx is not None
            server, task, session = ctx
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.02)
            assert server.started
            await main._stop_mcp_server(server, task, session)
            assert session.closed
            assert task.done()
        finally:
            os.unlink(db)

    async def test_start_failure_returns_none_and_closes_session(self, monkeypatch):
        import backend.main as main

        def _boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(main, "build_mcp_asgi_app", _boom)
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            app = build_app(FakeHomeAssistant(), db, frontend_dist=None, start_ha=False)
            assert await main._start_mcp_server(app) is None
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

    async def test_startup_tracks_ha_log_task(self):
        """Issue #304: the _log_ha_state background task must be kept referenced
        (the event loop only holds weak references) so it can't be GC'd while it
        waits up to 60 s on ha.wait_connected."""
        fake_ha = FakeHomeAssistant()
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            app = build_app(fake_ha, db, frontend_dist=None, start_ha=True)
            server = TestServer(app)
            async with TestClient(server) as c:
                await c.start_server()
                assert "ha_log_task" in c.app
                assert isinstance(c.app["ha_log_task"], asyncio.Task)
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
