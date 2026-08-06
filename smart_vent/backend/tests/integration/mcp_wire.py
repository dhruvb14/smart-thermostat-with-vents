"""A raw JSON-RPC MCP client for conformance testing (Issue #543).

Why this exists instead of using ``mcp.client.*``
-------------------------------------------------
This driver speaks the MCP Streamable HTTP **wire protocol** directly — plain
JSON-RPC over ``POST`` — and deliberately imports nothing from the ``mcp``
package. That is the whole point:

* **It survives the SDK migration.** ``mcp.client.streamable_http.streamablehttp_client``
  is *removed* in mcp v2. A conformance suite built on the SDK client would have
  to be rewritten as part of the very migration it is supposed to police, which
  destroys its value as a regression baseline — the "before" and "after" tests
  would no longer be the same tests. This driver is byte-identical across the
  v1 → v2 upgrade, so a behaviour change can only come from the *server*.
* **It can pin the protocol revision.** ``ClientSession.initialize()`` hardcodes
  ``protocolVersion=LATEST_PROTOCOL_VERSION`` (mcp 1.29 ``client/session.py``),
  so the SDK client cannot negotiate an older revision. Driving the wire lets us
  assert every revision the server claims to support, which is exactly the
  backward-compatibility guarantee mcp v2's dual-protocol support is sold on.
* **It exposes the session mechanics.** ``Mcp-Session-Id`` handling is explicit
  here, so a stateless server (no id issued) and a stateful one (id issued and
  echoed) are directly observable rather than hidden inside the SDK.

The tradeoff is that this driver is *not* a proof that a real SDK client works.
``test_mcp_conformance.py`` keeps one SDK-client test for that.
"""

from __future__ import annotations

import json
from typing import Any

import aiohttp

# The MCP Streamable HTTP transport requires clients to advertise that they can
# accept either a single JSON reply or an SSE stream, even when the server is
# configured for JSON responses.
_BASE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

# Every revision mcp 1.x lists in ``mcp.shared.version.SUPPORTED_PROTOCOL_VERSIONS``.
# Ordered oldest → newest. mcp v2 adds "2026-07-28" on top of these; the suite
# discovers what a server actually supports rather than assuming.
PROTOCOL_REVISIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")

# The revision a client gets if it never negotiates one. Kept here so the tests
# can assert the server's default rather than hardcoding it in three places.
OLDEST_SUPPORTED = PROTOCOL_REVISIONS[0]

# The revision mcp v2 introduces. Not in the 1.x list above, so PR-B adds it to
# PROTOCOL_REVISIONS once the server can actually speak it; until then the suite
# asserts only what the running server advertises.
V2_PROTOCOL_REVISION = "2026-07-28"


class McpProtocolError(RuntimeError):
    """A JSON-RPC level error (the ``error`` member was populated)."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


def _raise_for_jsonrpc_error(reply: dict[str, Any]) -> None:
    """Raise if *reply* carries a real JSON-RPC ``error`` object.

    Only a dict-shaped ``error`` is a JSON-RPC error. Transport-level
    rejections (a 401 from the bearer gate, a 503 from the ``mcp_enabled``
    toggle) return a plain ``{"error": "some message"}`` body, which callers
    inspect via ``last_status`` instead.
    """
    err = reply.get("error")
    if isinstance(err, dict):
        raise McpProtocolError(err.get("code", -1), err.get("message", ""), err.get("data"))


class RawMcpClient:
    """Minimal MCP Streamable HTTP client speaking raw JSON-RPC.

    One instance == one logical MCP session. Construct a fresh instance per
    protocol revision / per bearer token; instances are independent even when
    they share the underlying :class:`aiohttp.ClientSession`.
    """

    def __init__(
        self,
        http: aiohttp.ClientSession,
        url: str,
        *,
        bearer: str | None = None,
    ) -> None:
        self._http = http
        # The transport 307-redirects /mcp → /mcp/; requesting the slashed form
        # directly avoids a redirect on every single call.
        self.url = url if url.endswith("/") else url + "/"
        self.bearer = bearer
        self.session_id: str | None = None
        self.negotiated_protocol: str | None = None
        self.last_status: int | None = None
        # The HTTP status of the ``initialize`` POST specifically. Kept apart
        # from ``last_status`` because initialize() also fires the mandatory
        # ``notifications/initialized`` follow-up, whose 202 would otherwise
        # mask the handshake's own result (401 from the bearer gate, 503 from
        # the mcp_enabled toggle).
        self.init_status: int | None = None
        self._next_request_id = 0

    # -- plumbing ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = dict(_BASE_HEADERS)
        if self.bearer:
            headers["Authorization"] = f"Bearer {self.bearer}"
        # A stateful server issues a session id on initialize and requires it on
        # every subsequent request; a stateless one never issues one.
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        # Required by the 2025-06-18+ revisions on post-initialize requests.
        if self.negotiated_protocol:
            headers["MCP-Protocol-Version"] = self.negotiated_protocol
        return headers

    @staticmethod
    def _decode(text: str) -> dict[str, Any]:
        """Parse a reply that may be plain JSON or a single SSE ``data:`` frame."""
        text = text.strip()
        if text.startswith("data:"):
            for line in text.splitlines():
                if line.startswith("data:"):
                    framed: dict[str, Any] = json.loads(line[5:].strip())
                    return framed
            raise ValueError("SSE frame carried no data: line")
        plain: dict[str, Any] = json.loads(text)
        return plain

    async def _send(
        self, method: str, params: dict | None = None, *, notify: bool = False
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            self._next_request_id += 1
            payload["id"] = self._next_request_id

        async with self._http.post(
            self.url, json=payload, headers=self._headers(), allow_redirects=True
        ) as resp:
            self.last_status = resp.status
            # Header name is case-insensitive on the wire; aiohttp normalises.
            issued = resp.headers.get("Mcp-Session-Id")
            if issued and not self.session_id:
                self.session_id = issued
            body = await resp.text()

        if notify or not body.strip():
            return {}
        return self._decode(body)

    # -- protocol ---------------------------------------------------------

    async def initialize(self, protocol_version: str) -> dict[str, Any]:
        """Perform the ``initialize`` handshake requesting *protocol_version*.

        Returns the raw ``result`` object. Also sends the mandatory
        ``notifications/initialized`` follow-up so the session is usable.
        """
        reply = await self._send(
            "initialize",
            {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "plenum-conformance", "version": "1"},
            },
        )
        self.init_status = self.last_status
        _raise_for_jsonrpc_error(reply)
        # A transport-level rejection (401 from the auth gate, 503 from the
        # mcp_enabled toggle) is a plain JSON body, not a JSON-RPC envelope.
        # Return it unchanged so the caller can assert on ``init_status``.
        result: dict[str, Any] = reply.get("result", {})
        if not result:
            return reply
        self.negotiated_protocol = result.get("protocolVersion")
        await self._send("notifications/initialized", {}, notify=True)
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        reply = await self._send("tools/list", {})
        _raise_for_jsonrpc_error(reply)
        tools: list[dict[str, Any]] = reply.get("result", {}).get("tools", [])
        return tools

    async def call_tool_raw(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool and return the raw JSON-RPC reply (``result`` OR ``error``).

        Deliberately does *not* raise on a JSON-RPC error: the v1 → v2 migration
        changes which failures surface as an error-flagged tool *result* versus a
        top-level JSON-RPC *error*, and the tests need to observe that difference
        rather than have it converted into an exception.
        """
        return await self._send("tools/call", {"name": name, "arguments": arguments})


def tool_result_text(reply: dict[str, Any]) -> str:
    """Concatenate the text content blocks of a ``tools/call`` reply.

    Tolerates both the v1 and v2 content-block shapes so the assertion helpers
    do not need to branch on SDK version.
    """
    result = reply.get("result") or {}
    chunks: list[str] = []
    for block in result.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            chunks.append(block.get("text", ""))
    return "".join(chunks)


def tool_result_json(reply: dict[str, Any]) -> Any:
    """Parse the tool result's text payload as JSON."""
    return json.loads(tool_result_text(reply))


def tool_failed(reply: dict[str, Any]) -> bool:
    """True if the call failed, by EITHER mechanism.

    A tool failure can surface two ways, and which one is used is exactly what
    the v1 → v2 migration changes:

    * v1: an error-flagged tool result (``result.isError``), and
    * v2: a top-level JSON-RPC ``error`` member for unexpected exceptions.

    Tests that only care *that* it failed use this; the tests that pin down
    *which* mechanism was used inspect the reply directly.
    """
    if "error" in reply:
        return True
    result = reply.get("result") or {}
    # v1 spells it isError; v2 renames the Python attribute to is_error but the
    # wire format stays camelCase. Accept both so this helper is version-proof.
    return bool(result.get("isError") or result.get("is_error"))
