"""
Add-on entry point.

Starts:
- HA WebSocket client (background task)
- Scheduler (background task, 60s ticks)
- aiohttp server (REST API + WebSocket + static frontend)
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp_apispec import setup_aiohttp_apispec
from dotenv import load_dotenv

from .api.routes import routes
from .api.ws_handler import WSManager
from .event_logger import EventLogger
from .ha_client import HAClient, build_ha_client
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

    # OpenAPI / Swagger UI
    from aiohttp_apispec.aiohttp_apispec import AiohttpApiSpec

    original_get_index_page = AiohttpApiSpec._get_index_page

    def patched_get_index_page(self, app_obj, static_files, static_path_str):
        html = original_get_index_page(self, app_obj, static_files, static_path_str)
        # Make paths relative for Home Assistant Ingress compatibility
        return html.replace('"/api/docs/', '"./')

    AiohttpApiSpec._get_index_page = patched_get_index_page

    log.info("OpenAPI Documentation (Swagger) enabled at /api/docs/")
    setup_aiohttp_apispec(
        app=app,
        title="Plenum API",
        version="v1",
        url="/api/docs/openapi.json",
        swagger_path="/api/docs/",
        static_path="/api/docs/static",
    )
    # Redirect without trailing slash to ensure relative paths work
    app.router.add_get("/api/docs", lambda r: web.HTTPFound("/api/docs/"))  # type: ignore[arg-type, return-value]

    if frontend_dist is not None and frontend_dist.exists():
        app.router.add_static("/assets", frontend_dist / "assets")

        async def spa_handler(request: web.Request) -> web.Response:
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
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        import uvloop

        uvloop.run(main())
    except ImportError:
        asyncio.run(main())
