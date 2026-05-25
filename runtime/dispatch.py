"""Catch-all dispatcher for ``/integrations/<name>/<tail>``.

One aiohttp route is registered at startup; per-request routing happens
here against the runtime-mutable registry in :mod:`runtime.api`. This
sidesteps aiohttp's "frozen Application after AppRunner.setup()"
constraint, so integrations can be added, replaced, or removed while
the server is live.
"""

from __future__ import annotations

import logging

from aiohttp import web

from . import api as _api
from .common import json_error

logger = logging.getLogger(__name__)


async def _dispatch_integration(request: web.Request) -> web.StreamResponse:
    name = request.match_info.get("name", "")
    tail = "/" + request.match_info.get("tail", "")

    router = _api.lookup(name)
    if router is None:
        return json_error(404, f"unknown integration: {name!r}")

    # 405-aware: if a path matched but the method didn't, say so explicitly
    # rather than masquerading as a 404 — easier to debug on the client side.
    path_matched = False
    for method, pattern, handler in router.routes():
        m = pattern.fullmatch(tail)
        if m is None:
            continue
        if method != request.method:
            path_matched = True
            continue
        # Merge path-param captures into match_info. The catch-all's own
        # ``name`` and ``tail`` keys stay (integration handlers can still
        # see them); collisions on those keys are caller error.
        request.match_info.update(m.groupdict())
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception:
            logger.exception(
                "[backplane] integration %s handler crashed (%s %s)",
                name,
                request.method,
                request.rel_url.raw_path,
            )
            return json_error(500, "integration handler crashed")

    if path_matched:
        return json_error(405, "method not allowed")
    return json_error(404, f"no route matches {request.method} {tail}")


def register_dispatcher(app: web.Application) -> None:
    """Install the single catch-all route on the main app.

    Called once during :func:`runtime.http_app.build_http_app` while the
    app is still mutable. After this, every register / replace / remove
    on an integration takes effect on the next matching request without
    touching the aiohttp Application again.
    """
    app.router.add_route(
        "*", "/integrations/{name}/{tail:.*}", _dispatch_integration
    )
