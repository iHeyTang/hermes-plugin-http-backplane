"""HTTP application factory.

Composes the backplane's HTTP surface from native feature lanes plus
dynamically-registered integration plugins.
"""

from __future__ import annotations

from aiohttp import web

from .common import json_error
from .features import register_native
from .features.hermes_proxy.attachments.routes import max_client_size_bytes
from .features.integrations import load_all as load_integrations
from .integrations_mount import mount_all, teardown


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
    # Discover presets + user integrations and queue each via
    # register_integration() before we drain. Errors in any single
    # integration are logged + skipped inside load_all — they must not
    # take the HTTP server down.
    load_integrations()
    # Drain the integration-registration queue and mount each onto
    # /integrations/<name>/* as a sub-application. After this call, any
    # subsequent register_integration() in the same process mounts
    # immediately rather than queueing.
    mount_all(app)
    app.on_shutdown.append(teardown)
    app.router.add_route(
        "OPTIONS", "/{path_info:.*}", lambda _req: web.Response(status=204)
    )
    return app
