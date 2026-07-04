#!/usr/bin/env python3
"""ws_watch.py — pure-stdlib watcher for Plenum's /ws push stream.

Usage:
    python3 ws_watch.py ws://HOST:8099/ws [--type zone_status] [--raw]

Connects to the aiohttp WebSocket endpoint (server → client push only; the
server sends protocol pings every 30 s — this client answers with pongs so the
connection stays alive). Prints one line per event:

    HH:MM:SS zone_status  climate.upstairs  state=running temp=71.2 sp=72.0
    HH:MM:SS log_event    WARNING [engine] Sensor stale for room Office

Event types pushed by the backend (v0.22.1): zone_status (per-engine snapshot
from cycle_engine._maybe_broadcast), log_event (every EventLogger row), plus
system_enabled_changed / dev_mode_changed / mcp_enabled_changed toggles.
Temperatures in zone_status are raw °F.

--type filters to one event type; --raw prints the full JSON payload.
No external deps: implements a minimal RFC 6455 client over a plain socket
(no TLS — use it against the addon port / port-forward, not an https ingress URL).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import sys
import time
from datetime import datetime
from urllib.parse import urlparse


def handshake(sock: socket.socket, host: str, path: str) -> None:
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("server closed during handshake")
        resp += chunk
    status = resp.split(b"\r\n", 1)[0].decode()
    if "101" not in status:
        raise ConnectionError(f"handshake rejected: {status}")


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


def send_frame(sock: socket.socket, opcode: int, payload: bytes = b"") -> None:
    # Client frames MUST be masked (RFC 6455 §5.3).
    mask = os.urandom(4)
    header = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header += bytes([0x80 | n])
    elif n < 65536:
        header += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        header += bytes([0x80 | 127]) + struct.pack(">Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(header + mask + masked)


def read_frame(sock: socket.socket) -> tuple[int, bytes]:
    b1, b2 = recv_exact(sock, 2)
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", recv_exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", recv_exact(sock, 8))[0]
    mask = recv_exact(sock, 4) if masked else b""
    payload = recv_exact(sock, n) if n else b""
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def render(msg: dict) -> str:
    ts = datetime.now().strftime("%H:%M:%S")
    etype = msg.get("type", "?")
    d = msg.get("data", {}) or {}
    if etype == "zone_status":
        return (f"{ts} zone_status  {d.get('thermostat_entity_id')}  "
                f"state={d.get('cycle_state')} action={d.get('hvac_action')} "
                f"temp={d.get('current_temp')} sp={d.get('setpoint')} "
                f"cycle={d.get('cycle_id')}")
    if etype == "log_event":
        return (f"{ts} log_event    {str(d.get('level', '')).upper():<7} "
                f"[{d.get('category')}] {d.get('message')}")
    return f"{ts} {etype}  {json.dumps(d)}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url", help="e.g. ws://192.168.1.10:8099/ws")
    ap.add_argument("--type", dest="etype", default=None, help="filter to one event type")
    ap.add_argument("--raw", action="store_true", help="print full JSON")
    args = ap.parse_args()

    u = urlparse(args.url)
    if u.scheme != "ws":
        print("only ws:// is supported (no TLS)", file=sys.stderr)
        return 2
    host, port, path = u.hostname, u.port or 80, u.path or "/ws"

    while True:
        try:
            sock = socket.create_connection((host, port), timeout=90)
            handshake(sock, f"{host}:{port}", path)
            print(f"# connected to {args.url}", file=sys.stderr)
            fragments = b""
            while True:
                opcode, payload = read_frame(sock)
                if opcode == 0x9:  # ping -> pong
                    send_frame(sock, 0xA, payload)
                    continue
                if opcode == 0x8:  # close
                    raise ConnectionError("server closed")
                if opcode in (0x1, 0x0):  # text / continuation
                    fragments += payload
                    try:
                        msg = json.loads(fragments.decode())
                    except ValueError:
                        continue  # incomplete fragmented message
                    fragments = b""
                    if args.etype and msg.get("type") != args.etype:
                        continue
                    print(json.dumps(msg) if args.raw else render(msg), flush=True)
        except KeyboardInterrupt:
            return 0
        except (OSError, ConnectionError) as exc:
            print(f"# disconnected ({exc}); retrying in 3s", file=sys.stderr)
            time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
