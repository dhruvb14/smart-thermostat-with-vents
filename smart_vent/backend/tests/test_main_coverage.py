"""
Tests for backend/main.py and backend/api/ws_handler.py.

Covers: build_app startup/shutdown paths, the security_headers middleware,
the SPA handler, and WSManager broadcast/handle.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from backend.api.ws_handler import WSManager
from backend.main import _apply_security_headers, _migrate_db_filename, build_app

from .integration.fake_ha import FakeHomeAssistant

# ---------------------------------------------------------------------------
# _migrate_db_filename
# ---------------------------------------------------------------------------


class TestMigrateDbFilename:
    def test_no_files_is_noop(self, tmp_path):
        """A fresh install has no legacy `flair.db`: the migration must leave
        the data dir exactly as it found it — in particular it must not create
        an empty `app.db` that would then look like a real (corrupt) database.
        """
        (tmp_path / "unrelated.txt").write_bytes(b"keep me")

        _migrate_db_filename(str(tmp_path))  # must not raise

        assert sorted(p.name for p in tmp_path.iterdir()) == ["unrelated.txt"]

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
# _resolve_require_auth + build_app auth wiring (Issue #373)
# ---------------------------------------------------------------------------


class TestResolveRequireAuth:
    def test_truthy_values(self, monkeypatch):
        from backend.main import _resolve_require_auth

        for val in ("true", "True", "1", "yes", "on", " TRUE "):
            monkeypatch.setenv("REQUIRE_AUTH", val)
            assert _resolve_require_auth() is True

    def test_falsy_and_absent_values(self, monkeypatch):
        from backend.main import _resolve_require_auth

        for val in ("false", "False", "0", "no", "", "  ", "off", " OFF "):
            monkeypatch.setenv("REQUIRE_AUTH", val)
            assert _resolve_require_auth() is False
        monkeypatch.delenv("REQUIRE_AUTH", raising=False)
        assert _resolve_require_auth() is False

    def test_unrecognized_value_refuses_to_start(self, monkeypatch):
        # A typo in an auth toggle must not silently fail open (#499): a
        # malformed value refuses to start instead of disabling the boundary.
        from backend.main import _resolve_require_auth

        for val in ("treu", "nonsense", "2", "enabled"):
            monkeypatch.setenv("REQUIRE_AUTH", val)
            with pytest.raises(SystemExit, match="Invalid REQUIRE_AUTH"):
                _resolve_require_auth()

    def test_build_app_enables_auth_from_env(self, tmp_path, monkeypatch, caplog):
        import logging

        monkeypatch.setenv("REQUIRE_AUTH", "true")
        # DATA_DIR points the session secret at a writable temp dir.
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        fake_ha = FakeHomeAssistant()
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            with caplog.at_level(logging.INFO, logger="backend.main"):
                app = build_app(fake_ha, db, frontend_dist=None, start_ha=False)
            assert app["require_auth"] is True
            assert isinstance(app["session_secret"], bytes)
            assert any("Authentication ENABLED" in r.message for r in caplog.records)
        finally:
            os.unlink(db)

    def test_build_app_disabled_by_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("REQUIRE_AUTH", raising=False)
        fake_ha = FakeHomeAssistant()
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            app = build_app(fake_ha, db, frontend_dist=None, start_ha=False)
            assert app["require_auth"] is False
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
        """on_shutdown must cancel BOTH background tasks. Leaving them running
        keeps the HA reconnect loop and the 60 s wait_connected watcher alive
        past teardown."""
        never = asyncio.Event()  # never set → both tasks stay pending

        class NeverConnectingHA(FakeHomeAssistant):
            async def start(self) -> None:
                await never.wait()

            async def wait_connected(self, timeout: float = 30.0) -> None:
                await never.wait()

        fake_ha = NeverConnectingHA()
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            app = build_app(fake_ha, db, frontend_dist=None, start_ha=True)
            server = TestServer(app)
            async with TestClient(server) as c:
                await c.start_server()
                tasks = [c.app["ha_task"], c.app["ha_log_task"]]
                await asyncio.sleep(0)
                assert not any(t.done() for t in tasks), "tasks must still be pending"
            # Context exit ran on_shutdown; let the cancellations land.
            for _ in range(5):
                await asyncio.sleep(0)
            assert [t.cancelled() for t in tasks] == [True, True]
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

    async def test_spa_handler_serves_public_dir_asset(self, tmp_path):
        """Vite public/ files (e.g. apple-touch-icon.png) land at the dist
        root, not under /assets — the SPA handler must serve them as files
        rather than falling back to index.html."""
        fake_ha = FakeHomeAssistant()
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            dist = tmp_path / "dist"
            dist.mkdir()
            (dist / "assets").mkdir()
            (dist / "index.html").write_text("<html>SPA</html>")
            (dist / "apple-touch-icon.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-icon-bytes")

            app = build_app(fake_ha, db, frontend_dist=dist, start_ha=False)
            server = TestServer(app)
            async with TestClient(server) as c:
                await c.start_server()
                resp = await c.get("/apple-touch-icon.png")
                assert resp.status == 200
                body = await resp.read()
                assert body == b"\x89PNG\r\n\x1a\nfake-icon-bytes"
        finally:
            os.unlink(db)

    async def test_spa_handler_blocks_path_traversal(self, tmp_path):
        """A tail like ../secrets.txt must not escape frontend_dist."""
        fake_ha = FakeHomeAssistant()
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            dist = tmp_path / "dist"
            dist.mkdir()
            (dist / "assets").mkdir()
            (dist / "index.html").write_text("<html>SPA</html>")
            (tmp_path / "secret.txt").write_text("do not serve me")

            app = build_app(fake_ha, db, frontend_dist=dist, start_ha=False)
            server = TestServer(app)
            async with TestClient(server) as c:
                await c.start_server()
                resp = await c.get("/../secret.txt")
                assert resp.status == 200
                text = await resp.text()
                assert "SPA" in text
        finally:
            os.unlink(db)


# ---------------------------------------------------------------------------
# WSManager
# ---------------------------------------------------------------------------


class TestWSManagerBroadcast:
    async def test_broadcast_to_no_clients_is_noop(self):
        """With an empty client set the send loop never runs, so nothing is
        sent and nothing is pruned — the registry is still usable afterwards.
        """
        mgr = WSManager()

        await mgr.broadcast("test_event", {"key": "val"})  # must not raise

        assert list(mgr._clients) == []

        # And the manager is not left in a broken state: a client registered
        # after the empty broadcast still receives the next one.
        class _Recorder:
            def __init__(self) -> None:
                self.sent: list[str] = []

            async def send_str(self, message: str) -> None:
                self.sent.append(message)

        ws = _Recorder()
        mgr._clients.add(ws)
        await mgr.broadcast("second", {"key": "val2"})
        assert [json.loads(m) for m in ws.sent] == [{"type": "second", "data": {"key": "val2"}}]

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


# ---------------------------------------------------------------------------
# Coverage additions: CSRF WS-upgrade guard, supervisor detection, HA-connect
# timeout logging, WS ERROR frames, and main()'s bind/cleanup lifecycle
# ---------------------------------------------------------------------------


class TestCsrfWebSocketUpgrade:
    async def test_ws_upgrade_without_origin_is_forbidden(self):
        """A WebSocket upgrade with no Origin header must be rejected (CSRF /
        cross-site WebSocket hijacking guard)."""
        fake_ha = FakeHomeAssistant()
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            app = build_app(fake_ha, db, frontend_dist=None, start_ha=False)
            server = TestServer(app)
            async with TestClient(server) as c:
                await c.start_server()
                resp = await c.get(
                    "/ws",
                    headers={"Upgrade": "websocket", "Connection": "Upgrade"},
                )
                assert resp.status == 403
                assert "missing Origin" in await resp.text()
        finally:
            os.unlink(db)


class TestSupervisorDetection:
    async def test_resolved_supervisor_ip_is_stored(self, monkeypatch, caplog):
        import logging

        import backend.main as main_mod

        monkeypatch.setattr(main_mod, "resolve_supervisor_ip", lambda: "172.30.32.2")
        fake_ha = FakeHomeAssistant()
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            with caplog.at_level(logging.INFO, logger="backend.main"):
                app = build_app(fake_ha, db, frontend_dist=None, start_ha=False)
            assert app["supervisor_ip"] == "172.30.32.2"
            assert any("Supervisor resolved at 172.30.32.2" in r.message for r in caplog.records)
        finally:
            os.unlink(db)


class TestHaConnectTimeoutLogging:
    async def test_wait_connected_timeout_writes_warning_event(self):
        """When HA doesn't connect within the startup window, the background
        logger task must record a warning event instead of raising."""

        class _NeverConnects(FakeHomeAssistant):
            async def wait_connected(self, timeout: float = 30.0) -> None:
                raise TimeoutError

        fake_ha = _NeverConnects()
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            app = build_app(fake_ha, db, frontend_dist=None, start_ha=True)
            server = TestServer(app)
            async with TestClient(server) as c:
                await c.start_server()
                await c.app["ha_log_task"]  # let the logger task finish
                conn = c.app["scheduler"]._db_conn
                async with conn.execute("SELECT level, category, message FROM event_log") as cur:
                    rows = await cur.fetchall()
                assert any(
                    r["level"] == "warning"
                    and r["category"] == "ha"
                    and "not yet connected" in r["message"]
                    for r in rows
                )
        finally:
            os.unlink(db)


class TestWSManagerErrorFrame:
    async def test_error_frame_breaks_loop_and_discards_client(self, monkeypatch):
        """An ERROR frame must terminate the receive loop and unregister the
        client so broadcasts stop targeting it."""
        from aiohttp import WSMsgType

        from backend.api import ws_handler as ws_mod

        received: list = []

        class _FakeWS:
            closed = False

            async def prepare(self, request):
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not received:
                    received.append("error-frame")
                    return type("Msg", (), {"type": WSMsgType.ERROR})()
                raise AssertionError("loop must break on the ERROR frame")

        fake_ws = _FakeWS()
        monkeypatch.setattr(ws_mod.web, "WebSocketResponse", lambda *a, **k: fake_ws, raising=True)
        mgr = WSManager()
        result = await mgr.handle(MagicMockRequest())
        assert result is fake_ws
        assert received == ["error-frame"]  # exactly one frame consumed
        assert len(mgr._clients) == 0  # discarded on exit


class MagicMockRequest:
    """Minimal request stand-in — WSManager.handle only passes it to prepare()."""


class TestMainLifecycle:
    async def test_main_binds_starts_mcp_and_cleans_up(self, monkeypatch, tmp_path):
        """main() must bind the site, start the MCP server, and on shutdown stop
        the MCP server and clean the runner up."""
        from unittest.mock import AsyncMock

        import backend.main as main_mod

        fake_ha = FakeHomeAssistant()
        monkeypatch.setattr(main_mod, "DATA_DIR", str(tmp_path))
        monkeypatch.setattr(main_mod, "DB_PATH", str(tmp_path / "app.db"))
        monkeypatch.setattr(main_mod, "PORT", 0)  # ephemeral port
        monkeypatch.setattr(main_mod, "build_ha_client", lambda: fake_ha)
        mcp_sentinel = ("server", "task", "session")
        start_mcp = AsyncMock(return_value=mcp_sentinel)
        stop_mcp = AsyncMock()
        monkeypatch.setattr(main_mod, "_start_mcp_server", start_mcp)
        monkeypatch.setattr(main_mod, "_stop_mcp_server", stop_mcp)

        task = asyncio.get_running_loop().create_task(main_mod.main())
        # Wait until the MCP server has been started — the site is bound by then.
        for _ in range(200):
            if start_mcp.await_count:
                break
            await asyncio.sleep(0.05)
        assert start_mcp.await_count == 1

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        stop_mcp.assert_awaited_once_with(*mcp_sentinel)
        assert (tmp_path / "app.db").exists()  # DB was created in DATA_DIR


# ---------------------------------------------------------------------------
# MQTT bridge wiring (Issue #519)
#
# The bridge itself is exercised in test_mqtt_*.py; these cover how main.py
# wires it up — the loopback dispatcher it is given, and the rule that nothing
# about MQTT may ever take the add-on down.
# ---------------------------------------------------------------------------


class TestMqttBridgeLifecycle:
    async def test_not_started_when_no_broker_is_configured(self, monkeypatch, tmp_path):
        """No Supervisor and no MQTT_HOST: there is nothing to connect to. This
        is the ONLY unavailable state — there is no mqtt_enabled deployment
        gate, so a HAOS install with Mosquitto is available on first boot."""
        import backend.main as main

        monkeypatch.delenv("MQTT_HOST", raising=False)
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        app = build_app(
            FakeHomeAssistant(), str(tmp_path / "a.db"), frontend_dist=None, start_ha=False
        )
        assert await main._start_mqtt_bridge(app) is None
        # The resolved config is still exposed so the Settings panel can explain why.
        assert app["mqtt"]["config"].configured is False

    async def test_starts_and_stops_cleanly(self, monkeypatch, tmp_path):
        """A resolvable broker alone makes the bridge available; the Settings
        toggle decides whether it connects."""
        import backend.main as main

        monkeypatch.setenv("MQTT_HOST", "broker.invalid")
        app = build_app(
            FakeHomeAssistant(), str(tmp_path / "b.db"), frontend_dist=None, start_ha=False
        )
        ctx = await main._start_mqtt_bridge(app)
        assert ctx is not None
        bridge, task, session = ctx
        assert app["mqtt"]["bridge"] is bridge
        await main._stop_mqtt_bridge(bridge, task, session)
        assert session.closed
        assert task.done()

    async def test_start_failure_returns_none_and_closes_the_session(self, monkeypatch, tmp_path):
        """MQTT is optional; a failure here must never propagate."""
        import backend.main as main

        monkeypatch.setenv("MQTT_HOST", "broker.invalid")

        def _boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(main, "MqttBridge", _boom)
        app = build_app(
            FakeHomeAssistant(), str(tmp_path / "c.db"), frontend_dist=None, start_ha=False
        )
        assert await main._start_mqtt_bridge(app) is None

    async def test_broadcast_pokes_the_bridge_for_a_prompt_resync(self, tmp_path):
        """A UI or engine change must reach MQTT without waiting out the refresh
        interval, so the existing broadcast doubles as the change signal."""
        app = build_app(
            FakeHomeAssistant(), str(tmp_path / "d.db"), frontend_dist=None, start_ha=False
        )

        class _Bridge:
            poked = False

            def request_sync(self):
                self.poked = True

        bridge = _Bridge()
        app["mqtt"]["bridge"] = bridge
        await app["scheduler"]._broadcast("anything", {})
        assert bridge.poked is True


class TestLoopbackDispatch:
    async def test_dispatches_through_the_real_rest_api(self, tmp_path):
        """The whole design rests on this: MQTT reaches the same route handlers
        REST does, so validation and unit conversion cannot drift."""
        import backend.main as main

        app = build_app(
            FakeHomeAssistant(), str(tmp_path / "e.db"), frontend_dist=None, start_ha=False
        )
        server = TestServer(app)
        async with TestClient(server) as client:
            await client.start_server()
            import aiohttp

            base = f"http://127.0.0.1:{client.server.port}"
            async with aiohttp.ClientSession() as session:
                dispatch = main.build_loopback_dispatch(session, base, app["internal_token"])
                status, payload = await dispatch("GET", "/api/system/status", None)
                assert status == 200 and payload["enabled"] is True

                status, payload = await dispatch(
                    "POST", "/api/rooms", {"name": "Office", "thermostat_entity_id": "climate.x"}
                )
                assert status == 201 and payload["name"] == "Office"

    async def test_carries_the_internal_token_and_a_write_scope(self, tmp_path):
        """The internal token clears CSRF and marks the call as in-process; the
        scope header caps MQTT at `write` when require_auth is on, so
        destructive operations stay unreachable from a broker."""
        import aiohttp
        from aiohttp import web

        import backend.main as main
        from backend import scopes

        seen: dict = {}

        async def _echo(request: web.Request) -> web.Response:
            seen.update(dict(request.headers))
            return web.json_response({"ok": True})

        probe = web.Application()
        probe.router.add_route("*", "/api/system/status", _echo)
        server = TestServer(probe)
        async with TestClient(server) as client:
            await client.start_server()
            base = f"http://127.0.0.1:{client.server.port}"
            async with aiohttp.ClientSession() as session:
                dispatch = main.build_loopback_dispatch(session, base, "the-token")
                await dispatch("GET", "/api/system/status", None)

        assert seen["X-Plenum-Internal"] == "the-token"
        assert seen["X-Plenum-Scope"] == scopes.WRITE
        # Never `destructive` — deleting rooms and restoring backups are not on
        # the MQTT surface and must not become reachable through it.
        assert seen["X-Plenum-Scope"] != scopes.DESTRUCTIVE

    async def test_scope_blocks_a_destructive_call_when_auth_is_on(self, tmp_path):
        """The header is not decoration: with require_auth on, the REST auth
        middleware refuses a destructive path presented with `write`."""
        import aiohttp

        import backend.main as main

        app = build_app(
            FakeHomeAssistant(), str(tmp_path / "f.db"), frontend_dist=None, start_ha=False
        )
        app["require_auth"] = True
        server = TestServer(app)
        async with TestClient(server) as client:
            await client.start_server()
            base = f"http://127.0.0.1:{client.server.port}"
            async with aiohttp.ClientSession() as session:
                dispatch = main.build_loopback_dispatch(session, base, app["internal_token"])
                status, _ = await dispatch("POST", "/api/restore", {})
                assert status == 403
                # A plain write still goes through.
                status, _ = await dispatch("POST", "/api/system/enabled", {"enabled": False})
                assert status == 200

    async def test_unreachable_api_yields_a_generic_error(self, tmp_path):
        """Never leak the underlying exception onto a public MQTT topic."""
        import aiohttp

        import backend.main as main

        async with aiohttp.ClientSession() as session:
            dispatch = main.build_loopback_dispatch(session, "http://127.0.0.1:1", "tok")
            status, payload = await dispatch("GET", "/api/system/status", None)
        assert status == 503
        assert payload == {"error": "Failed to reach the Plenum API"}

    async def test_non_json_response_does_not_raise(self, tmp_path):
        import aiohttp

        import backend.main as main

        app = build_app(
            FakeHomeAssistant(), str(tmp_path / "g.db"), frontend_dist=None, start_ha=False
        )
        server = TestServer(app)
        async with TestClient(server) as client:
            await client.start_server()
            base = f"http://127.0.0.1:{client.server.port}"
            async with aiohttp.ClientSession() as session:
                dispatch = main.build_loopback_dispatch(session, base, app["internal_token"])
                status, payload = await dispatch("GET", "/api/metrics/export.csv", None)
        assert status == 200 and payload is None
