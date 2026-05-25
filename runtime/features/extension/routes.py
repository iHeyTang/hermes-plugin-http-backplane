from __future__ import annotations

from urllib.parse import unquote

from aiohttp import web

from ...common import json_error
from .attachment_service import (
    MAX_ATTACHMENT_BYTES,
    build_attachment_upload_response,
    delete_attachment,
    delete_attachment_session,
)


async def handle_attach_upload(request: web.Request) -> web.Response:
    session_id = request.query.get("session_id")
    name = unquote(request.query.get("name", "file"))
    mime = unquote(request.query.get("mime", "")) or request.content_type
    if not mime:
        mime = "application/octet-stream"

    data = await request.read()
    try:
        result = build_attachment_upload_response(
            session_id=session_id,
            name=name,
            mime=mime,
            content_length=request.content_length,
            data=data,
        )
        return web.json_response(result)
    except ValueError as exc:
        return json_error(400, str(exc))
    except OverflowError as exc:
        return json_error(413, str(exc))


async def handle_attach_delete(request: web.Request) -> web.Response:
    """DELETE /extension/attach?path=<absolute-path>"""
    path = request.query.get("path", "")
    try:
        return web.json_response(delete_attachment(path))
    except PermissionError as exc:
        return json_error(403, str(exc))


async def handle_attach_delete_session(request: web.Request) -> web.Response:
    """DELETE /extension/attach/session/{session_id}"""
    session_id = request.match_info.get("session_id", "")
    try:
        return web.json_response(delete_attachment_session(session_id))
    except PermissionError as exc:
        return json_error(403, str(exc))


def max_client_size_bytes() -> int:
    return MAX_ATTACHMENT_BYTES


def register(app: web.Application) -> None:
    app.add_routes(
        [
            web.post("/extension/attach", handle_attach_upload),
            web.delete("/extension/attach", handle_attach_delete),
            web.delete(
                "/extension/attach/session/{session_id}",
                handle_attach_delete_session,
            ),
        ]
    )
