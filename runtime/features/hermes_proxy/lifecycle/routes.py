from __future__ import annotations

from aiohttp import web

from ....common import json_error
from .service import (
    ACTION_LOG_FILES,
    action_status,
    spawn_hermes_action,
    status_response,
)


def _parse_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.lower() in {"1", "true", "yes", "on"}


async def handle_status(request: web.Request) -> web.Response:
    force = _parse_bool(request.query.get("force_update_check"))
    return web.json_response(
        await status_response(force_update_check=force)
    )


async def handle_gateway_restart(_request: web.Request) -> web.Response:
    try:
        proc = spawn_hermes_action(["gateway", "restart"], "gateway-restart")
    except Exception as exc:
        return json_error(500, f"failed to restart gateway: {exc}")
    return web.json_response({"ok": True, "pid": proc.pid, "name": "gateway-restart"})


async def handle_update(_request: web.Request) -> web.Response:
    try:
        proc = spawn_hermes_action(["update"], "hermes-update")
    except Exception as exc:
        return json_error(500, f"failed to start update: {exc}")
    return web.json_response({"ok": True, "pid": proc.pid, "name": "hermes-update"})


def _parse_int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


async def handle_action_status(request: web.Request) -> web.Response:
    name = request.match_info.get("name", "")
    if name not in ACTION_LOG_FILES:
        return json_error(404, f"unknown action: {name}")
    lines = _parse_int(request.query.get("lines"), 200)
    payload = action_status(name, lines)
    if payload is None:
        return json_error(404, f"unknown action: {name}")
    return web.json_response(payload)


def register(app: web.Application) -> None:
    app.add_routes(
        [
            web.get("/hermes/status", handle_status),
            web.post("/hermes/gateway/restart", handle_gateway_restart),
            web.post("/hermes/update", handle_update),
            web.get("/hermes/actions/{name}/status", handle_action_status),
        ]
    )
