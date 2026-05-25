"""HTTP application factory.

Composes the backplane's HTTP surface from native feature lanes plus a
single catch-all dispatcher for ``/integrations/<name>/*``. The
dispatcher reads a runtime-mutable registry (see :mod:`runtime.api`),
so register / replace / remove on integrations works at any time —
boot, mid-flight, or post-tool-call — without re-touching the aiohttp
Application.
"""

from __future__ import annotations

from aiohttp import web

from .common import json_error
from .dispatch import register_dispatcher
from .features import register_native
from .features.hermes_proxy.attachments.routes import max_client_size_bytes
from .features.integrations import load_all as load_integrations


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as exc:
            if exc.content_type == "application/json":
                resp = exc
            else:
                resp = json_error(exc.status, exc.reason)

    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, PATCH, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


def build_http_app() -> web.Application:
    app = web.Application(
        middlewares=[cors_middleware], client_max_size=max_client_size_bytes()
    )
    register_native(app)
    register_dispatcher(app)
    # Populate the integration registry from built-in presets + user
    # integrations under ~/.hermes/integrations/. Any failures inside
    # a single integration are logged + skipped by load_all — must not
    # take the HTTP server down. After this returns the app is safe to
    # be frozen by AppRunner.setup(); subsequent register_integration
    # calls (e.g. from integration_install) go straight to the runtime
    # dict and become live on the next matching request.
    load_integrations()
    app.router.add_route(
        "OPTIONS", "/{path_info:.*}", lambda _req: web.Response(status=204)
    )
    return app
