"""
Test to ensure all REST API endpoints have proper OpenAPI documentation.
"""

from __future__ import annotations

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

    assert (
        not missing_schema
    ), f"The following endpoints are missing @response_schema decorators: {missing_schema}"
