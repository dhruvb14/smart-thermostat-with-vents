#!/usr/bin/env python3
"""
Creates an HA admin user + long-lived access token for E2E tests.

Handles first-time onboarding (fresh HA install) and re-runs (user exists).
Writes the long-lived token to --output so downstream scripts / docker-compose
can pick it up via:

    HA_TOKEN=$(cat /token/ha_token.txt)

Usage:
    python3 setup-ha.py --ha-url http://localhost:8123 --output /tmp/ha_token.txt
"""
import argparse
import sys
import time

import requests


def wait_for_ha(base_url: str, timeout: int = 120) -> None:
    print(f"Waiting for Home Assistant at {base_url} ...", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url}/api/config", timeout=5)
            # 200 = auth disabled, 401 = auth required — both mean HA is up
            if r.status_code in (200, 401):
                print("Home Assistant is up.", flush=True)
                return
        except Exception:
            pass
        time.sleep(3)
    raise SystemExit(f"ERROR: HA not ready after {timeout}s")


def onboarding_steps(base_url: str) -> list:
    try:
        r = requests.get(f"{base_url}/api/onboarding", timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return []


def onboard(base_url: str) -> str:
    """Complete HA first-run onboarding and return a short-lived access token."""
    print("Completing first-run onboarding ...", flush=True)
    r = requests.post(
        f"{base_url}/api/onboarding/users",
        json={
            "client_id": f"{base_url}/",
            "name": "E2E Admin",
            "username": "e2e_admin",
            "password": "e2e_password",
            "language": "en",
        },
        timeout=30,
    )
    r.raise_for_status()
    auth_code = r.json()["auth_code"]
    return _exchange_code(base_url, auth_code)


def login(base_url: str) -> str:
    """Log in with existing credentials and return a short-lived access token."""
    print("Logging in with existing credentials ...", flush=True)
    flow_r = requests.post(
        f"{base_url}/auth/login_flow",
        json={
            "client_id": f"{base_url}/",
            "handler": ["homeassistant", None],
            "redirect_uri": f"{base_url}/",
        },
        timeout=30,
    )
    flow_r.raise_for_status()
    flow_id = flow_r.json()["flow_id"]

    cred_r = requests.post(
        f"{base_url}/auth/login_flow/{flow_id}",
        json={"username": "e2e_admin", "password": "e2e_password"},
        timeout=30,
    )
    cred_r.raise_for_status()
    result = cred_r.json()

    # Some HA versions return auth_code; others return result directly
    auth_code = result.get("result") or result.get("auth_code")
    if not auth_code:
        raise SystemExit(f"ERROR: unexpected login response: {result}")
    return _exchange_code(base_url, auth_code)


def _exchange_code(base_url: str, auth_code: str) -> str:
    r = requests.post(
        f"{base_url}/auth/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": f"{base_url}/",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def create_long_lived_token(base_url: str, short_token: str) -> str:
    """Create a long-lived token via the HA WebSocket API.

    The REST endpoint /api/auth/long_lived_access_token was removed in HA 2024.7.
    The WebSocket API (auth/long_lived_access_token command) is the supported path.
    """
    print("Creating long-lived access token via WebSocket ...", flush=True)
    import json
    import websocket  # websocket-client package

    ws_url = (
        base_url.replace("http://", "ws://").replace("https://", "wss://")
        + "/api/websocket"
    )
    ws = websocket.create_connection(ws_url, timeout=30)
    try:
        # HA sends auth_required immediately on connect
        msg = json.loads(ws.recv())
        if msg.get("type") != "auth_required":
            raise SystemExit(f"ERROR: unexpected WS message: {msg}")

        # Authenticate with the short-lived bearer token
        ws.send(json.dumps({"type": "auth", "access_token": short_token}))
        msg = json.loads(ws.recv())
        if msg.get("type") != "auth_ok":
            raise SystemExit(f"ERROR: WebSocket auth failed: {msg}")

        # Request a long-lived token
        ws.send(
            json.dumps(
                {
                    "id": 1,
                    "type": "auth/long_lived_access_token",
                    "client_name": "e2e-test",
                    "lifespan": 3650,
                }
            )
        )
        msg = json.loads(ws.recv())
        if not msg.get("success"):
            raise SystemExit(f"ERROR: failed to create long-lived token: {msg}")
        return msg["result"]
    finally:
        ws.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ha-url", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    wait_for_ha(args.ha_url)

    steps = onboarding_steps(args.ha_url)
    needs_onboarding = any(not s.get("done") for s in steps) if steps else True

    short_token = onboard(args.ha_url) if needs_onboarding else login(args.ha_url)
    ll_token = create_long_lived_token(args.ha_url, short_token)

    with open(args.output, "w") as f:
        f.write(ll_token)

    print(f"Token written to {args.output}", flush=True)


if __name__ == "__main__":
    main()
