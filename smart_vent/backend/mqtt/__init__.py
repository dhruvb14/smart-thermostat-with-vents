"""MQTT interface for Home Assistant automations (Issue #519).

A third transport alongside REST and MCP. Home Assistant automations speak MQTT
natively (``mqtt.publish`` actions, and real entities via MQTT Discovery), which
neither REST (needs a hand-written ``rest_command:``) nor MCP (not a thing the
automation UI speaks) can offer.

Every command is dispatched back through the running REST API over loopback,
exactly as ``mcp_http.dispatch_tool`` does — so validation, the ``_to_f`` /
``_delta_to_f`` write boundary, ``event_log`` entries, and WebSocket broadcasts
all happen once, in the route handler. There is no second copy of the business
logic on the MQTT path.

Module map:
  ``config``     — resolving/sanitising the topic prefix and broker settings
  ``naming``     — the shared sanitisation rule (prefix, room names, entity ids)
  ``registry``   — the declarative table of every exposed control
  ``topics``     — building and parsing topic strings
  ``discovery``  — HA MQTT Discovery config payloads
  ``bridge``     — the runtime: subscribe, dispatch, publish state
"""

from __future__ import annotations
