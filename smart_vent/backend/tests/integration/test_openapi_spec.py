"""CI guard: committed openapi.json must match the live application spec.

If this test fails, run `python generate_spec.py` from smart_vent/ to refresh.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient

SPEC_FILE = Path(__file__).parents[3] / "openapi.json"


async def test_openapi_spec_is_current(client: TestClient) -> None:
    """openapi.json in the repo must exactly match /api/docs/openapi.json."""
    resp = await client.get("/api/docs/openapi.json")
    assert resp.status == 200
    live = await resp.json()

    if not SPEC_FILE.exists():
        pytest.fail(
            f"openapi.json not found at {SPEC_FILE}. "
            "Run `python generate_spec.py` from smart_vent/ to generate it."
        )

    committed = json.loads(SPEC_FILE.read_text())
    assert live == committed, (
        "openapi.json is stale. Run `python generate_spec.py` from smart_vent/ to refresh it."
    )
