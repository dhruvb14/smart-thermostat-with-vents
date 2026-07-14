"""Integration tests for MCP token management + dual-layer scope enforcement
(Issue #373, Phase 4).

Two halves:
1. The /api/mcp/tokens CRUD routes (mint shows the secret once; list never
   leaks it; revoke works; validation).
2. Scope enforcement at the REST boundary — the layer that actually gates MCP
   access, since MCP dispatches to the REST API over loopback. Exercised both
   directly (internal token + X-Plenum-Scope header) and end-to-end through the
   real MCP dispatcher.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import aiohttp
from aiohttp.test_utils import TestClient

from backend import db, mcp_http
from backend.mcp_openapi import build_tool_specs

# --------------------------------------------------------------------------
# Token CRUD (require_auth off — testing the route logic itself)
# --------------------------------------------------------------------------


async def test_mint_list_revoke(client: TestClient) -> None:
    resp = await client.post("/api/mcp/tokens", json={"label": "my token", "scope": "read"})
    assert resp.status == 201
    body = await resp.json()
    raw = body["token"]
    token_id = body["id"]
    assert raw and body["scope"] == "read" and body["label"] == "my token"

    # List returns metadata only — never the secret or its hash.
    resp = await client.get("/api/mcp/tokens")
    tokens = await resp.json()
    assert len(tokens) == 1
    assert tokens[0]["id"] == token_id
    assert "token" not in tokens[0]
    assert "token_hash" not in tokens[0]

    # Only the SHA-256 hash of the raw secret is stored.
    conn = client.app["scheduler"]._db_conn
    row = await db.get_mcp_token_by_hash(conn, hashlib.sha256(raw.encode()).hexdigest())
    assert row is not None and row["scope"] == "read"

    # Revoke.
    assert (await client.delete(f"/api/mcp/tokens/{token_id}")).status == 200
    assert await (await client.get("/api/mcp/tokens")).json() == []


async def test_mint_validation(client: TestClient) -> None:
    assert (await client.post("/api/mcp/tokens", json={"scope": "read"})).status == 400
    assert (
        await client.post("/api/mcp/tokens", json={"label": "  ", "scope": "read"})
    ).status == 400
    assert (
        await client.post("/api/mcp/tokens", json={"label": "x", "scope": "admin"})
    ).status == 400


async def test_revoke_missing_is_404(client: TestClient) -> None:
    assert (await client.delete("/api/mcp/tokens/nope")).status == 404


async def test_validate_mcp_bearer(client: TestClient) -> None:
    """The 9099-layer validator: a minted token's raw secret resolves to its
    scope and records last-used; an unknown token resolves to None."""
    from backend.main import validate_mcp_bearer

    scheduler = client.app["scheduler"]
    body = await (
        await client.post("/api/mcp/tokens", json={"label": "t", "scope": "write"})
    ).json()
    raw = body["token"]

    assert await validate_mcp_bearer(scheduler, raw) == "write"
    tokens = await (await client.get("/api/mcp/tokens")).json()
    assert tokens[0]["last_used_at"] is not None  # touch recorded

    assert await validate_mcp_bearer(scheduler, "not-a-real-token") is None


async def test_each_mint_is_unique(client: TestClient) -> None:
    a = await (await client.post("/api/mcp/tokens", json={"label": "a", "scope": "read"})).json()
    b = await (await client.post("/api/mcp/tokens", json={"label": "b", "scope": "write"})).json()
    assert a["token"] != b["token"]
    assert a["id"] != b["id"]


# --------------------------------------------------------------------------
# REST-layer scope enforcement (require_auth on, loopback header path)
# --------------------------------------------------------------------------


def _loopback(client: TestClient, scope: str) -> dict[str, str]:
    return {"X-Plenum-Internal": client.app["internal_token"], "X-Plenum-Scope": scope}


async def test_read_scope_blocks_writes(make_client: Callable) -> None:
    client = await make_client(require_auth=True)
    # read → GET ok
    assert (await client.get("/api/rooms", headers=_loopback(client, "read"))).status == 200
    # read → POST forbidden
    resp = await client.post(
        "/api/rooms",
        json={"name": "X", "thermostat_entity_id": "climate.x"},
        headers=_loopback(client, "read"),
    )
    assert resp.status == 403
    assert "scope" in (await resp.json())["error"]


async def test_write_scope_allows_writes(make_client: Callable) -> None:
    client = await make_client(require_auth=True)
    resp = await client.post(
        "/api/rooms",
        json={"name": "Y", "thermostat_entity_id": "climate.y"},
        headers=_loopback(client, "write"),
    )
    assert resp.status == 201


async def test_destructive_scope_required_for_backup(make_client: Callable) -> None:
    client = await make_client(require_auth=True)
    # /api/backup is a GET but destructive — write is insufficient.
    assert (await client.get("/api/backup", headers=_loopback(client, "write"))).status == 403
    assert (await client.get("/api/backup", headers=_loopback(client, "read"))).status == 403
    assert (await client.get("/api/backup", headers=_loopback(client, "destructive"))).status == 200


async def test_internal_token_without_scope_header_is_full_access(make_client: Callable) -> None:
    """Legacy loopback (no X-Plenum-Scope) keeps full access — used when
    require_auth is off; the header is what constrains MCP callers."""
    client = await make_client(require_auth=True)
    headers = {"X-Plenum-Internal": client.app["internal_token"]}
    assert (await client.get("/api/backup", headers=headers)).status == 200


async def test_token_management_needs_destructive_scope(make_client: Callable) -> None:
    client = await make_client(require_auth=True)
    # Managing tokens over MCP is destructive-scoped.
    assert (await client.get("/api/mcp/tokens", headers=_loopback(client, "write"))).status == 403
    assert (
        await client.get("/api/mcp/tokens", headers=_loopback(client, "destructive"))
    ).status == 200


# --------------------------------------------------------------------------
# End-to-end dual layer: the real MCP dispatcher honours the granted scope
# --------------------------------------------------------------------------


async def test_dispatch_respects_scope(make_client: Callable) -> None:
    client = await make_client(require_auth=True)
    base = str(client.make_url(""))
    tok = client.app["internal_token"]
    specs = {s.name: s for s in build_tool_specs(client.app)}
    room = {"name": "Z", "thermostat_entity_id": "climate.z"}

    async with aiohttp.ClientSession() as sess:
        # A read-scoped MCP call to a write tool → forbidden (isError, HTTP 403).
        mcp_http._mcp_scope.set("read")
        res: Any = await mcp_http.dispatch_tool(sess, base, specs["post_rooms"], room, tok)
        assert res.isError is True
        assert "403" in res.content[0].text

        # A write-scoped call to the same tool → success.
        mcp_http._mcp_scope.set("write")
        ok: Any = await mcp_http.dispatch_tool(sess, base, specs["post_rooms"], room, tok)
        assert isinstance(ok, list)  # list of content blocks, not an error

        # A write-scoped call to a destructive tool (get_backup) → forbidden.
        mcp_http._mcp_scope.set("write")
        res2: Any = await mcp_http.dispatch_tool(sess, base, specs["get_backup"], {}, tok)
        assert res2.isError is True
        assert "403" in res2.content[0].text
        # Reset so the ContextVar doesn't leak into other tests on this loop.
        mcp_http._mcp_scope.set(None)
