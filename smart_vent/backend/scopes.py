"""MCP token scopes (Issue #373, Phase 4).

Three scopes, ordered by privilege: ``read`` < ``write`` < ``destructive``. A
minted MCP token carries exactly one scope and may perform any operation whose
*required* scope is at or below it.

Scope classification is a **pure function of the REST method + path**, so the
exact same rule is used in two places (the campaign's dual-layer requirement):

* the MCP dispatcher computes a tool's required scope, and
* the auth middleware enforces it at the REST boundary — the layer that actually
  gates access, because MCP dispatches to the REST API over loopback. Guarding
  only the 9099 ASGI layer would be bypassable; the REST-side check is the one
  that counts.

Web sessions and ingress callers are unaffected by scopes — they are full-access
(admin) and never present an MCP token.
"""

from __future__ import annotations

READ = "read"
WRITE = "write"
DESTRUCTIVE = "destructive"
VALID_SCOPES = (READ, WRITE, DESTRUCTIVE)

_RANK = {READ: 0, WRITE: 1, DESTRUCTIVE: 2}

# Operations that hand over the whole system or the whole database — the highest
# bar regardless of HTTP verb. /api/backup is a GET but streams the entire
# app.db, so it is destructive despite being a read verb (#373 called this out).
_DESTRUCTIVE_PATHS = frozenset(
    {
        "/api/backup",
        "/api/restore",
        "/api/restart",
    }
)
# Minting/revoking MCP credentials is itself a destructive-level operation, so a
# lesser token cannot use these over MCP (a web admin always can).
_DESTRUCTIVE_PREFIXES = ("/api/mcp/tokens",)


def required_scope(method: str, path: str) -> str:
    """The minimum scope required to call ``method`` on ``path``."""
    if path in _DESTRUCTIVE_PATHS or path.startswith(_DESTRUCTIVE_PREFIXES):
        return DESTRUCTIVE
    return READ if method.upper() in ("GET", "HEAD") else WRITE


def scope_satisfies(granted: str, required: str) -> bool:
    """True iff a token with ``granted`` scope may perform a ``required`` op.

    Unknown/garbage granted scope never satisfies anything; an unknown required
    scope is treated as un-satisfiable (fail closed).
    """
    if granted not in _RANK or required not in _RANK:
        return False
    return _RANK[granted] >= _RANK[required]
