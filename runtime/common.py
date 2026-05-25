"""HTTP helpers shared across feature modules."""

from __future__ import annotations

import json
from typing import Any, Dict

from aiohttp import web

MAX_JSON_BODY_BYTES = 64 * 1024


def json_error(status: int, message: str) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


async def read_json_object(
    request: web.Request,
    *,
    max_bytes: int = MAX_JSON_BODY_BYTES,
) -> Dict[str, Any]:
    """Read and decode a JSON-object request body.

    The optional *max_bytes* override exists for endpoints that legitimately
    carry larger payloads (e.g. ``/hermes/sessions/{id}/messages`` can hold
    an assistant turn with a long reasoning trace or a tool result with a
    full page's text). The aiohttp app-level ``client_max_size`` still caps
    the absolute ceiling; this parameter just relaxes the inner JSON-body
    guard rail.
    """
    raw_body = await request.read()
    if not raw_body:
        raise web.HTTPBadRequest(
            text=json.dumps({"ok": False, "error": "empty body"}),
            content_type="application/json",
        )
    if len(raw_body) > max_bytes:
        raise web.HTTPBadRequest(
            text=json.dumps(
                {"ok": False, "error": f"body too large (max {max_bytes} bytes)"}
            ),
            content_type="application/json",
        )
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise web.HTTPBadRequest(
            text=json.dumps({"ok": False, "error": f"invalid JSON: {exc}"}),
            content_type="application/json",
        )
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(
            text=json.dumps({"ok": False, "error": "JSON object required"}),
            content_type="application/json",
        )
    return payload
