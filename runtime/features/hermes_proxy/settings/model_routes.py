from __future__ import annotations

from urllib.parse import unquote

from aiohttp import web

from .model_catalog_service import (
    build_model_catalog_response,
    build_provider_models_http_response,
)
from ....common import json_error


async def handle_model_catalog(request: web.Request) -> web.Response:
    refresh_raw = request.query.get("refresh", "0")
    force_refresh = str(refresh_raw).lower() in ("1", "true", "yes")
    body = build_model_catalog_response(force_refresh=force_refresh)
    return web.json_response(body)


async def handle_provider_models(request: web.Request) -> web.Response:
    refresh_pm = request.query.get("refresh", "0")
    force_pm = str(refresh_pm).lower() in ("1", "true", "yes")
    provider_pm = unquote(str(request.query.get("provider", ""))).strip()
    if not provider_pm:
        return json_error(400, "missing provider query parameter")
    body_pm = build_provider_models_http_response(
        provider=provider_pm,
        force_refresh=force_pm,
    )
    return web.json_response(body_pm)


def register_model_routes(app: web.Application) -> None:
    app.add_routes(
        [
            web.get("/hermes/model-catalog", handle_model_catalog),
            web.get("/hermes/provider-models", handle_provider_models),
        ]
    )

