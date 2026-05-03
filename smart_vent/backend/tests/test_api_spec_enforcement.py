"""
Test to ensure all REST API endpoints have proper OpenAPI documentation.
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.api.routes import routes


def test_all_routes_documented() -> None:
    """
    Iterate through all registered routes and assert they have @docs metadata.
    This ensures that any new endpoint added without decorators will fail the build.
    """
    # Exclude non-API routes if any (e.g., static files, SPA catch-all)
    # API routes start with /api/
    api_routes = [r for r in routes if r.path.startswith("/api/")]

    missing_docs = []

    for route in api_routes:
        handler = route.handler
        # aiohttp-apispec attaches metadata to the handler function
        # usually in handler.__apispec__
        if not hasattr(handler, "__apispec__"):
            missing_docs.append(f"{route.method} {route.path}")

    assert not missing_docs, f"The following endpoints are missing @docs decorators: {missing_docs}"


def test_all_routes_have_response_schema() -> None:
    """
    Ensure all API routes have a response schema defined.
    """
    api_routes = [r for r in routes if r.path.startswith("/api/")]

    # Exceptions: endpoints that return raw files or non-JSON content
    EXCEPTIONS = {"/api/backup", "/api/metrics/export.csv"}

    missing_schema = []

    for route in api_routes:
        if route.path in EXCEPTIONS:
            continue

        handler = route.handler
        spec = getattr(handler, "__apispec__", {})

        # aiohttp-apispec 2.x+ stores response in spec['responses']
        # We check if at least one successful response (200 or 201) is documented
        responses = spec.get("responses", {})
        if not any(code in responses for code in ("200", "201", 200, 201)):
            missing_schema.append(f"{route.method} {route.path}")

    assert not missing_schema, (
        f"The following endpoints are missing @response_schema decorators: {missing_schema}"
    )


def test_api_ts_contract() -> None:
    """Every /api/ path in api.ts must have a matching registered backend route.

    Catches drift where frontend calls an endpoint that no longer exists on the backend.
    """
    api_ts = Path(__file__).parent.parent.parent / "frontend" / "src" / "api.ts"
    assert api_ts.exists(), f"api.ts not found at {api_ts}"
    content = api_ts.read_text()

    frontend_paths: set[str] = set()

    # Static string paths: "/api/..."
    for m in re.finditer(r'"(/api/[^"?#]*)"', content):
        frontend_paths.add(m.group(1).rstrip("/"))

    # Template literal paths — normalize ${...} → {p}, strip query strings
    for m in re.finditer(r"`(/api/[^`]*)`", content):
        raw = m.group(1)
        # Replace well-formed ${...} expressions with a placeholder
        simplified = re.sub(r"\$\{[^{}]*\}", "{p}", raw)
        # Strip trailing {p} (represents an optional query-string expression at the end)
        simplified = re.sub(r"(\{p\})+$", "", simplified)
        # Split at any remaining ${ (malformed from nested templates) or literal ?
        clean = re.split(r"\$\{|\?", simplified)[0].rstrip("/")
        if clean.startswith("/api/"):
            frontend_paths.add(clean)

    def _norm(path: str) -> str:
        """Normalize aiohttp path params {name} / {name:pattern} → {p}."""
        return re.sub(r"\{[^}]+\}", "{p}", path)

    backend_paths = {_norm(r.path) for r in routes}

    missing = [
        fp
        for fp in sorted(frontend_paths)
        if not any(
            bn == fp or bn.startswith(fp + "/") or bn.startswith(fp + "/{p}")
            for bn in backend_paths
        )
    ]

    assert not missing, "The following api.ts paths have no matching backend route:\n" + "\n".join(
        f"  {p}" for p in missing
    )
