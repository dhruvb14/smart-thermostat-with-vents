"""Enforce that every query parameter a route handler reads is declared via
``@query_params`` (Issue #403).

The MCP tool surface is generated from the OpenAPI spec, which now models query
parameters — but only the ones a handler *declares*. A handler that reads
``request.rel_url.query.get("foo")`` without declaring ``foo`` leaves that knob
invisible over MCP (and undocumented in Swagger). This test statically scans
each ``/api/`` handler's source for the query keys it reads and asserts they are
all declared, so new endpoints cannot regress the exposure.

Detection is AST-based and covers the two read forms used in the codebase:
``<expr>.query.get("k")`` / ``.getone`` / ``.getall`` and ``<expr>.query["k"]``,
including the ``q = request.rel_url.query`` alias pattern.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from backend.main import build_app
from backend.tests.integration.fake_ha import FakeHomeAssistant

_QUERY_READ_METHODS = {"get", "getone", "getall"}


def _query_alias_names(tree: ast.AST) -> set[str]:
    """Local names bound to ``<expr>.query`` (e.g. ``q = request.rel_url.query``)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "query"
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _is_query_receiver(node: ast.expr, aliases: set[str]) -> bool:
    """True if *node* refers to a query multidict (``.query`` attr or an alias)."""
    if isinstance(node, ast.Attribute) and node.attr == "query":
        return True
    return isinstance(node, ast.Name) and node.id in aliases


def _read_query_keys(func) -> set[str]:
    """Static set of query-string keys *func* reads with a string literal."""
    src = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(src)
    aliases = _query_alias_names(tree)
    keys: set[str] = set()
    for node in ast.walk(tree):
        # <query>.get("k") / .getone("k") / .getall("k")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _QUERY_READ_METHODS
            and node.args
            and _is_query_receiver(node.func.value, aliases)
        ):
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                keys.add(arg.value)
        # <query>["k"]
        if isinstance(node, ast.Subscript) and _is_query_receiver(node.value, aliases):
            sl = node.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                keys.add(sl.value)
    return keys


def test_every_read_query_param_is_declared_for_mcp() -> None:
    app = build_app(FakeHomeAssistant(), ":memory:", frontend_dist=None, start_ha=False)  # type: ignore[arg-type]

    problems: list[str] = []
    seen: set = set()
    for route in app.router.routes():
        handler = route.handler
        meta = getattr(handler, "__apispec__", None)
        if not meta:
            continue
        resource = route.resource
        path = resource.canonical if resource is not None else None
        if not path or not path.startswith("/api/"):
            continue
        if handler in seen:
            continue
        seen.add(handler)

        read = _read_query_keys(handler)
        declared = {p["name"] for p in meta.get("query_params", [])}
        missing = read - declared
        if missing:
            problems.append(
                f"{route.method} {path} reads undeclared query params: {sorted(missing)}"
            )

    assert not problems, (
        "Every query param a handler reads must be declared via @query_params so it "
        "is exposed over OpenAPI/MCP (Issue #403):\n" + "\n".join(problems)
    )


def test_detector_finds_alias_and_subscript_reads() -> None:
    # Guards the detector itself so a false-negative can't hide a real gap.
    async def handler(request):
        q = request.rel_url.query
        _a = q.get("aliased")
        _b = request.rel_url.query.get("direct")
        _c = request.query["subscript"]
        _d = q.getall("multi")
        return _a, _b, _c, _d

    assert _read_query_keys(handler) == {"aliased", "direct", "subscript", "multi"}
