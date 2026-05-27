"""HTTP Bearer auth middleware for the backplane.

Reads ``HERMES_BACKPLANE_KEY`` from the environment per request. When
set, every request must carry ``Authorization: Bearer <key>``; on
mismatch the middleware returns 401. When the env is unset, all
requests are accepted — preserves the loopback-implicit-trust behaviour
the backplane had before any auth was introduced.

Exempt routes (always allowed regardless of the env var):

- ``OPTIONS *`` — CORS preflight; the browser sends this without auth.
- ``/health`` — liveness probe consumed by the extension's onboarding
  gate to distinguish "backplane is down" (connection refused) from
  "backplane is up but the key is wrong" (401).
"""

from __future__ import annotations

import os

from aiohttp import web

from .common import json_error

BACKPLANE_KEY_ENV = "HERMES_BACKPLANE_KEY"

_AUTH_EXEMPT_PATHS = frozenset({"/health"})


def _expected_key() -> str:
    return (os.environ.get(BACKPLANE_KEY_ENV) or "").strip()


def _extract_bearer(header_value: str) -> str:
    if not header_value:
        return ""
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


@web.middleware
async def auth_middleware(request: web.Request, handler):
    expected = _expected_key()
    if not expected:
        return await handler(request)
    if request.method == "OPTIONS" or request.path in _AUTH_EXEMPT_PATHS:
        return await handler(request)
    presented = _extract_bearer(request.headers.get("Authorization", ""))
    if presented != expected:
        return json_error(401, "missing or invalid HERMES_BACKPLANE_KEY")
    return await handler(request)
