from __future__ import annotations

from aiohttp import web

from ....common import json_error
from .service import (
    LogsError,
    available_components,
    available_log_files,
    read_logs,
)


def _parse_int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


async def handle_logs(request: web.Request) -> web.Response:
    q = request.query
    try:
        payload = read_logs(
            file=q.get("file") or "agent",
            lines=_parse_int(q.get("lines"), 100),
            level=q.get("level"),
            component=q.get("component"),
            search=q.get("search"),
        )
    except LogsError as exc:
        return json_error(exc.status, exc.message)
    return web.json_response(payload)


async def handle_log_meta(_request: web.Request) -> web.Response:
    """Expose the whitelists so the UI can build pickers without hardcoding."""
    return web.json_response(
        {
            "files": available_log_files(),
            "components": available_components(),
        }
    )


def register(app: web.Application) -> None:
    app.add_routes(
        [
            web.get("/hermes/logs", handle_logs),
            web.get("/hermes/logs/meta", handle_log_meta),
        ]
    )
