"""
Add-on entry point.

Starts:
- HA WebSocket client (background task)
- Scheduler (background task, 60s ticks)
- aiohttp server (REST API + WebSocket + static frontend)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

from .api.openapi import setup_openapi
from .api.routes import routes
from .api.ws_handler import WSManager
from .event_logger import EventLogger
from .ha_client import HAClient, build_ha_client
from .mcp_http import build_mcp_asgi_app
from .scheduler import Scheduler

# Load .env for local development. In the HA add-on container this file
# doesn't exist; load_dotenv silently no-ops, and env vars come from the
# add-on's options (injected by run.sh).
load_dotenv(Path(__file__).parent.parent.parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "app.db")
PORT = int(os.environ.get("PORT", 8099))
# Dedicated port for the HTTP MCP server (native ASGI). Exposed via config.yaml
# `ports:` like the web UI; the endpoint is gated by the `mcp_enabled` toggle.
MCP_PORT = int(os.environ.get("MCP_PORT", 9099))
FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"


def _migrate_db_filename(data_dir: str) -> None:
    """One-shot rename of flair.db → app.db. Idempotent."""
    old = os.path.join(data_dir, "flair.db")
    new = os.path.join(data_dir, "app.db")
    if os.path.exists(old) and not os.path.exists(new):
        os.rename(old, new)
        # WAL/SHM sidecars can exist if HA killed us mid-write
        for suffix in ("-wal", "-shm"):
            old_side = os.path.join(data_dir, f"flair.db{suffix}")
            if os.path.exists(old_side):
                os.rename(old_side, os.path.join(data_dir, f"app.db{suffix}"))


def _apply_security_headers(headers: Any, request: web.Request) -> None:
    """Apply standard defense-in-depth security headers to a response header dict."""
    headers["X-Content-Type-Options"] = "nosniff"
    headers["X-Frame-Options"] = "SAMEORIGIN"
    headers["X-XSS-Protection"] = "1; mode=block"
    headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    # CSP: Allow 'self' for everything. 'unsafe-inline' is needed for standard React/Vite
    # builds that don't use nonces. 'self' is enough for WebSocket connections since they
    # go through the same origin/host.
    headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'self';"
    )

    # HSTS: Only if request is secure (SSL/TLS)
    if request.secure:
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    # Suppress Server header
    headers["Server"] = ""


@web.middleware
async def security_headers_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Add standard security headers to all responses (Defense in Depth)."""
    try:
        response: web.StreamResponse = await handler(request)
    except web.HTTPException as ex:
        # Re-apply headers even to error responses (404, 403, etc.)
        # Check prepared to avoid RuntimeError on some aiohttp versions/response types
        if not ex.prepared:
            _apply_security_headers(ex.headers, request)
        raise
    except Exception:
        # For unexpected errors that aren't HTTPErrors, aiohttp would return a
        # default 500 without our security headers. This catch-all ensures
        # headers are still present by returning a custom 500 response.
        log.exception("Unhandled exception in request handler")
        error_resp = web.Response(status=500, text="Internal Server Error")
        _apply_security_headers(error_resp.headers, request)
        return error_resp

    # Don't modify headers if response is already prepared (e.g. WebSocket)
    if not response.prepared:
        _apply_security_headers(response.headers, request)
    return response


def build_app(
    ha: HAClient,
    db_path: str,
    *,
    frontend_dist: Path | None = FRONTEND_DIST,
    start_ha: bool = True,
) -> web.Application:
    """Build the aiohttp application with all components wired.

    Shared between production (`main()`) and integration tests. Tests inject
    a fake HA client and point `db_path` at an in-memory DB, and pass
    ``start_ha=False`` so no background WS task is spawned.
    """
    ws_manager = WSManager()

    async def broadcast(event_type: str, payload: dict) -> None:
        await ws_manager.broadcast(event_type, payload)

    event_logger = EventLogger(broadcast=broadcast)
    scheduler = Scheduler(ha=ha, db_path=db_path, broadcast=broadcast, event_logger=event_logger)

    app = web.Application(middlewares=[security_headers_middleware])
    app["ha"] = ha
    app["scheduler"] = scheduler
    app["ws_manager"] = ws_manager
    app["event_logger"] = event_logger
    app["db_path"] = db_path

    app.add_routes(routes)
    app.router.add_get("/ws", ws_manager.handle)

    # OpenAPI / Swagger UI — generated directly from apispec + marshmallow
    # (Issue #188; replaces the abandoned aiohttp-apispec). The Swagger UI page
    # uses relative asset paths so it works behind Home Assistant ingress, and
    # also registers the /api/docs → /api/docs/ redirect.
    log.info("OpenAPI Documentation (Swagger) enabled at /api/docs/")
    setup_openapi(
        app,
        title="Plenum API",
        version="v1",
        url="/api/docs/openapi.json",
        swagger_path="/api/docs/",
        static_path="/api/docs/static",
    )

    if frontend_dist is not None and frontend_dist.exists():
        app.router.add_static("/assets", frontend_dist / "assets")
        dist_root = os.path.realpath(frontend_dist)

        async def spa_handler(request: web.Request) -> web.StreamResponse:
            # Vite's public/ dir (e.g. apple-touch-icon.png) is copied to the
            # dist root alongside index.html, not under /assets — serve those
            # files directly before falling back to the SPA shell so routes
            # like React Router paths still resolve to index.html. `tail` is
            # attacker-controlled, so resolve the real path and confirm it
            # stays strictly inside dist_root (realpath + startswith(base +
            # sep), not just is_relative_to — the latter isn't recognized as
            # a sanitizer by CodeQL's path-injection query) before any
            # filesystem access.
            tail = request.match_info.get("tail", "")
            if tail:
                candidate = os.path.realpath(os.path.join(dist_root, tail))
                if candidate.startswith(dist_root + os.sep) and os.path.isfile(candidate):
                    return web.FileResponse(candidate)

            index = frontend_dist / "index.html"
            return web.Response(
                body=index.read_bytes(),
                content_type="text/html",
            )

        app.router.add_get("/", spa_handler)
        app.router.add_get("/{tail:(?!api|ws|assets).*}", spa_handler)
    elif frontend_dist is not None:
        log.warning("Frontend dist not found at %s — API-only mode", frontend_dist)

    async def on_startup(app: web.Application) -> None:
        if start_ha:
            app["ha_task"] = asyncio.create_task(ha.start())
        await scheduler.start()

        if start_ha:

            async def _log_ha_state() -> None:
                try:
                    await ha.wait_connected(timeout=60)
                    await event_logger.log("info", "ha", "Connected to Home Assistant WebSocket")
                except TimeoutError:
                    await event_logger.log(
                        "warning",
                        "ha",
                        "HA WebSocket not yet connected — retrying in background",
                    )

            # Keep a strong reference — the event loop only holds weak refs, so
            # an un-referenced task can be GC'd mid-await (Issue #304).
            app["ha_log_task"] = asyncio.create_task(_log_ha_state())

    async def on_shutdown(app: web.Application) -> None:
        await scheduler.stop()
        if start_ha:
            await ha.stop()
        for key in ("ha_task", "ha_log_task"):
            task = app.get(key)
            if task:
                task.cancel()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


async def main() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    _migrate_db_filename(DATA_DIR)
    ha = build_ha_client()
    app = build_app(ha, DB_PATH)

    log.info("Starting Plenum on port %d — binding immediately", PORT)
    runner = web.AppRunner(app)
    await runner.setup()  # runs on_startup → scheduler is started after this
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    mcp_ctx = await _start_mcp_server(app)

    try:
        await asyncio.Event().wait()
    finally:
        if mcp_ctx is not None:
            await _stop_mcp_server(*mcp_ctx)
        await runner.cleanup()


async def _start_mcp_server(
    app: web.Application,
) -> tuple[Any, asyncio.Task, aiohttp.ClientSession] | None:
    """Launch the HTTP MCP server (uvicorn) on ``MCP_PORT`` as a background task.

    The server always binds; the ``/mcp`` endpoint itself is gated per-request by
    the ``mcp_enabled`` toggle, so users can flip it on/off without a restart.
    Any failure here is logged and swallowed — the MCP server is optional and
    must never take down the core add-on.
    """
    session: aiohttp.ClientSession | None = None
    try:
        import uvicorn

        session = aiohttp.ClientSession()
        base_url = f"http://127.0.0.1:{PORT}"
        scheduler = app["scheduler"]
        asgi_app = build_mcp_asgi_app(app, session, base_url, is_enabled=scheduler.get_mcp_enabled)
        config = uvicorn.Config(
            asgi_app,
            host="0.0.0.0",
            port=MCP_PORT,
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
        server = uvicorn.Server(config)
        # We manage shutdown ourselves; don't let uvicorn grab the process
        # signal handlers out from under aiohttp.
        server.install_signal_handlers = lambda: None  # type: ignore[attr-defined]
        task = asyncio.create_task(server.serve())
        log.info("HTTP MCP server listening on port %d at /mcp (toggle-gated)", MCP_PORT)
        return server, task, session
    except Exception:
        log.exception("Failed to start HTTP MCP server — continuing without it")
        if session is not None:
            await session.close()
        return None


async def _stop_mcp_server(server: Any, task: asyncio.Task, session: aiohttp.ClientSession) -> None:
    server.should_exit = True
    with contextlib.suppress(Exception):
        await task
    await session.close()


if __name__ == "__main__":
    try:
        import uvloop

        uvloop.run(main())
    except ImportError:
        asyncio.run(main())
