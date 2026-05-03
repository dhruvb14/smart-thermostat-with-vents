#!/usr/bin/env python3
"""Generate / refresh openapi.json from the live application spec.

Run from the smart_vent/ directory:
    python generate_spec.py

The script boots the app against an in-memory DB (no HA connection needed),
fetches /api/docs/openapi.json, and writes the result to smart_vent/openapi.json.
Commit the result so CI can diff it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

# Ensure the smart_vent package is importable when run as a script.
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


async def _fetch_spec() -> dict:
    from aiohttp.test_utils import TestClient, TestServer

    from backend.main import build_app
    from backend.tests.integration.fake_ha import FakeHomeAssistant

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        app = build_app(FakeHomeAssistant(), db_path, frontend_dist=None, start_ha=False)
        async with TestClient(TestServer(app)) as client:
            await client.start_server()
            resp = await client.get("/api/docs/openapi.json")
            if resp.status != 200:
                raise RuntimeError(f"Spec endpoint returned HTTP {resp.status}")
            return await resp.json()
    finally:
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                os.unlink(p)


def main() -> None:
    spec = asyncio.run(_fetch_spec())
    out = ROOT / "openapi.json"
    out.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    print(f"Written {out}")


if __name__ == "__main__":
    main()
