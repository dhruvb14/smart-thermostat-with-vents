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

from aiohttp import web

from .ha_client import build_ha_client
from .scheduler import Scheduler
from .api.routes import routes
from .api.ws_handler import WSManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "flair.db")
PORT = int(os.environ.get("PORT", 8099))
FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"


async def main() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)

    ha = build_ha_client()
    ws_manager = WSManager()

    async def broadcast(event_type: str, payload: dict) -> None:
        await ws_manager.broadcast(event_type, payload)

    scheduler = Scheduler(ha=ha, db_path=DB_PATH, broadcast=broadcast)

    app = web.Application()
    app["ha"] = ha
    app["scheduler"] = scheduler
    app["ws_manager"] = ws_manager

    # REST routes
    app.add_routes(routes)

    # WebSocket endpoint
    app.router.add_get("/ws", ws_manager.handle)

    # Serve frontend static files (Vite build output)
    if FRONTEND_DIST.exists():
        app.router.add_static("/assets", FRONTEND_DIST / "assets")

        async def spa_handler(request: web.Request) -> web.Response:
            """Serve index.html for any non-API path (SPA routing)."""
            index = FRONTEND_DIST / "index.html"
            return web.Response(
                body=index.read_bytes(),
                content_type="text/html",
            )

        app.router.add_get("/", spa_handler)
        app.router.add_get("/{tail:(?!api|ws|assets).*}", spa_handler)
    else:
        log.warning("Frontend dist not found at %s — API-only mode", FRONTEND_DIST)

    # Startup / shutdown hooks
    async def on_startup(app: web.Application) -> None:
        # Start HA client
        app["ha_task"] = asyncio.create_task(ha.start())
        log.info("Waiting for HA WebSocket connection…")
        try:
            await ha.wait_connected(timeout=30)
            log.info("HA connected")
        except asyncio.TimeoutError:
            log.warning("HA connection timed out — will keep retrying in background")
        # Start scheduler
        await scheduler.start()

    async def on_shutdown(app: web.Application) -> None:
        await scheduler.stop()
        await ha.stop()
        task = app.get("ha_task")
        if task:
            task.cancel()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    log.info("Starting Flair Replacement on port %d", PORT)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    # Run until interrupted
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
