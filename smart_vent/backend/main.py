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
import hashlib
import hmac
import logging
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from aiohttp.typedefs import Handler
from dotenv import load_dotenv

from . import auth, db, scopes, session, tz
from .api.openapi import setup_openapi
from .api.routes import routes
from .api.ws_handler import WSManager
from .auth import resolve_supervisor_ip
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
async def security_headers_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
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


@web.middleware
async def csrf_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Add standard CSRF protection to state-changing endpoints (CWE-352).

    This middleware requires that all state-changing requests (POST, PUT, DELETE,
    PATCH) or WebSocket upgrades either:
    1. Present the per-process internal token (the in-process MCP loopback), or
    2. Carry a custom request header a cross-origin browser cannot set without a
       (failing) CORS preflight, or
    3. Have an Origin header that matches the Host header.

    SECURITY BOUNDARY — READ BEFORE REUSING THESE HEADERS. The exempt request
    headers below are a *CSRF* signal only: they establish "a cross-origin
    browser could not have forged this," because browsers cannot set custom
    headers cross-origin without a preflight. They are NOT an authentication or
    trust signal — a non-browser client with socket access sets them freely
    (see the #373 spoofing analysis). The ingress-trust decision (auth.py)
    therefore keys on the unspoofable Supervisor peer-IP, never on these
    headers. Do not collapse the two header lists.
    """
    if (
        request.method in ("POST", "PUT", "DELETE", "PATCH")
        or request.headers.get("Upgrade") == "websocket"
    ):
        # In-process MCP loopback dispatch: authenticated by a per-process random
        # token (secrets.token_urlsafe, minted in build_app), NOT a guessable
        # static string. Compared in constant time. Unforgeable from off-process.
        internal_token = request.app.get("internal_token")
        presented = request.headers.get("X-Plenum-Internal", "")
        if internal_token and hmac.compare_digest(presented, internal_token):
            return await handler(request)

        # Custom headers trigger a CORS preflight that fails cross-origin, so
        # their presence proves the request is not a forged cross-site browser
        # request. CSRF-only — see the SECURITY BOUNDARY note above.
        exempt_headers = ("X-Requested-With", "X-Hass-Source", "X-Ingress-Path")
        if any(h in request.headers for h in exempt_headers):
            return await handler(request)

        origin = request.headers.get("Origin")
        host = request.host
        if origin:
            # Relaxed match: allow http://host or https://host
            if origin not in (f"http://{host}", f"https://{host}"):
                log.warning("CSRF check failed: Origin %s does not match Host %s", origin, host)
                raise web.HTTPForbidden(text="CSRF check failed")
        elif request.headers.get("Upgrade") == "websocket":
            # WebSockets are particularly vulnerable to hijacking; require an Origin.
            log.warning("CSRF check failed: WebSocket upgrade missing Origin header")
            raise web.HTTPForbidden(text="CSRF check failed (missing Origin)")

    return await handler(request)


def _resolve_require_auth() -> bool:
    """Whether the #373 auth boundary is enforced.

    Driven by the ``REQUIRE_AUTH`` env var, which ``run.sh`` exports from the
    ``require_auth`` add-on option (default ``true`` in ``config.yaml``). When
    the var is absent entirely — plain local dev and the pytest harness, which
    never run ``run.sh`` — it defaults to **False** so behavior is byte-for-byte
    identical to pre-#373 (the existing suite/curl-matrix stay green). Every real
    add-on / Docker deployment goes through ``run.sh`` and gets the secure
    default.
    """
    raw = os.environ.get("REQUIRE_AUTH", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


@web.middleware
async def auth_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Enforce the Issue #373 trust boundary. A no-op unless ``require_auth`` is on.

    Ordering (``middlewares=[security_headers, auth, csrf]``): auth sits between
    security headers (outer) and CSRF (inner) so a 401 still carries security
    headers, and the auth decision is made *before* any CSRF logic runs.

    Decision order for a guarded request:
      1. ``require_auth`` off → pass through (legacy open behavior).
      2. Unguarded path (SPA shell, static assets, health, ``/api/auth/*``) →
         pass through.
      3. In-process MCP loopback (per-boot ``internal_token``) → trusted; it
         originated inside our own process, not off-box. (Scope enforcement for
         MCP is layered on in Phase 4.)
      4. Home Assistant ingress (unspoofable Supervisor peer-IP) → auto-admin,
         no login — the ingress contract that must never break.
      5. A valid direct-port session cookie → pass through.
      6. Otherwise → 401.
    """
    if not request.app.get("require_auth"):
        return await handler(request)
    if not auth.is_protected_path(request.path):
        return await handler(request)

    internal_token = request.app.get("internal_token")
    presented = request.headers.get("X-Plenum-Internal", "")
    if internal_token and hmac.compare_digest(presented, internal_token):
        # In-process MCP loopback. Enforce the presenting token's scope at the
        # REST boundary (#373 dual-layer — the layer that actually gates access,
        # since MCP dispatch is loopback). The dispatcher threads the granted
        # scope via X-Plenum-Scope; when present, the endpoint's required scope
        # must be satisfied. Absent ⇒ legacy open (no MCP scope constraint).
        granted = request.headers.get("X-Plenum-Scope")
        if granted is not None:
            needed = scopes.required_scope(request.method, request.path)
            if not scopes.scope_satisfies(granted, needed):
                return web.json_response(
                    {
                        "error": (
                            f"MCP token scope '{granted}' is not sufficient for this "
                            f"'{needed}' operation"
                        )
                    },
                    status=403,
                )
        return await handler(request)

    if auth.is_ingress_request(request):
        return await handler(request)

    if session.session_user(request) is not None:
        return await handler(request)

    return auth.unauthorized()


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

    app = web.Application(
        middlewares=[security_headers_middleware, auth_middleware, csrf_middleware]
    )
    # Per-process secret shared with the in-process MCP loopback dispatcher so its
    # 127.0.0.1 REST calls satisfy CSRF without a guessable marker. Regenerated
    # each boot; never persisted, never leaves the process.
    app["internal_token"] = secrets.token_urlsafe(32)
    # Supervisor's address on the hassio network, resolved once at boot. Used by
    # auth.is_ingress_request to distinguish trusted ingress traffic from
    # direct-port callers (Issue #373). None when there is no Supervisor (local
    # dev / CI) — then nothing is classified as ingress.
    app["supervisor_ip"] = resolve_supervisor_ip()
    if app["supervisor_ip"]:
        log.info(
            "Supervisor resolved at %s — ingress requests will be auto-trusted",
            app["supervisor_ip"],
        )
    else:
        log.info("No Supervisor detected — all requests treated as direct (non-ingress)")
    # Whether the auth boundary is enforced (Issue #373). Resolved once at boot;
    # the middleware re-reads it per request off the app so tests can flip it.
    app["require_auth"] = _resolve_require_auth()
    # HMAC signing secret for direct-port session cookies. Persisted beside the
    # DB but NOT inside it, so a /api/backup download can't leak live sessions
    # (see backend/session.py). Loaded once at boot. For an in-memory DB (tests)
    # there is no on-disk sibling, so fall back to DATA_DIR / the temp dir rather
    # than scattering a .session_secret into the CWD.
    if db_path == ":memory:":
        _session_data_dir = os.environ.get("DATA_DIR") or tempfile.gettempdir()
    else:
        _session_data_dir = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(db_path))
    app["session_secret"] = session.load_or_create_secret(_session_data_dir)
    if app["require_auth"]:
        log.info("Authentication ENABLED (require_auth=true) — direct-port callers must log in")
    else:
        log.info("Authentication DISABLED (require_auth=false) — legacy open access")
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


async def validate_mcp_bearer(scheduler: Scheduler, token: str) -> str | None:
    """Return the granted scope for a presented MCP bearer token, or None (#373).

    Only the token's hash is stored, so we hash the presentation and look it up;
    a hit records last-used and yields the token's scope.
    """
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = await scheduler.get_db()
    row = await db.get_mcp_token_by_hash(conn, token_hash)
    if row is None:
        return None
    await db.touch_mcp_token(conn, token_hash, tz.now_utc().isoformat())
    return str(row["scope"])


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
        asgi_app = build_mcp_asgi_app(
            app,
            session,
            base_url,
            is_enabled=scheduler.get_mcp_enabled,
            internal_token=app["internal_token"],
            require_auth=lambda: bool(app.get("require_auth")),
            validate_bearer=lambda token: validate_mcp_bearer(scheduler, token),
        )
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
