from __future__ import annotations

from urllib.parse import unquote

from aiohttp import web

from .main_provider_settings_service import (
    read_main_provider_settings_response,
    save_main_provider_settings_response,
)
from .model_config_service import (
    read_auxiliary_models_response,
    read_main_model_response,
    write_auxiliary_models_response,
    write_main_model_response,
)
from ....common import json_error, read_json_object


async def handle_main_provider_settings_get(request: web.Request) -> web.Response:
    cred_for = unquote(str(request.query.get("provider", ""))).strip() or None
    return web.json_response(read_main_provider_settings_response(credentials_for=cred_for))


async def handle_main_provider_settings_post(request: web.Request) -> web.Response:
    payload = await read_json_object(request)
    try:
        return web.json_response(save_main_provider_settings_response(payload))
    except ValueError as exc:
        return json_error(400, str(exc))
    except RuntimeError as exc:
        return json_error(501, str(exc))
    except OSError as exc:
        return json_error(500, str(exc))


async def handle_main_model_get(_request: web.Request) -> web.Response:
    try:
        return web.json_response(read_main_model_response())
    except RuntimeError as exc:
        return json_error(501, str(exc))


async def handle_main_model_post(request: web.Request) -> web.Response:
    payload = await read_json_object(request)
    try:
        return web.json_response(write_main_model_response(payload))
    except ValueError as exc:
        return json_error(400, str(exc))
    except RuntimeError as exc:
        return json_error(501, str(exc))


async def handle_auxiliary_models_get(_request: web.Request) -> web.Response:
    try:
        return web.json_response(read_auxiliary_models_response())
    except RuntimeError as exc:
        return json_error(501, str(exc))


async def handle_auxiliary_models_post(request: web.Request) -> web.Response:
    payload = await read_json_object(request)
    try:
        return web.json_response(write_auxiliary_models_response(payload))
    except ValueError as exc:
        return json_error(400, str(exc))
    except RuntimeError as exc:
        return json_error(501, str(exc))


def register_config_routes(app: web.Application) -> None:
    app.add_routes(
        [
            web.get("/hermes/main-provider-settings", handle_main_provider_settings_get),
            web.post("/hermes/main-provider-settings", handle_main_provider_settings_post),
            web.get("/hermes/main-model", handle_main_model_get),
            web.post("/hermes/main-model", handle_main_model_post),
            web.get("/hermes/auxiliary-models", handle_auxiliary_models_get),
            web.post("/hermes/auxiliary-models", handle_auxiliary_models_post),
        ]
    )
