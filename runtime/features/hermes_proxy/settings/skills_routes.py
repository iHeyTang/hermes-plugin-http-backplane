from __future__ import annotations

from aiohttp import web

from .skills_service import (
    list_skill_files,
    list_skills_response,
    read_skill_file,
    toggle_skill,
)
from ....common import json_error, read_json_object


async def handle_skills_list(_request: web.Request) -> web.Response:
    try:
        return web.json_response(list_skills_response())
    except OSError as exc:
        return json_error(500, str(exc))


async def handle_skill_files(request: web.Request) -> web.Response:
    name = request.match_info.get("name", "")
    try:
        payload = list_skill_files(name)
    except OSError as exc:
        return json_error(500, str(exc))
    if not payload.get("ok"):
        return web.json_response(payload, status=404)
    return web.json_response(payload)


async def handle_skill_file(request: web.Request) -> web.Response:
    name = request.match_info.get("name", "")
    rel = request.query.get("path", "")
    try:
        payload = read_skill_file(name, rel)
    except OSError as exc:
        return json_error(500, str(exc))
    if not payload.get("ok"):
        # 404 for "not found"; 400 for traversal / bad path so the UI can
        # distinguish "the agent removed this file" from "the request was
        # malformed".
        msg = str(payload.get("error") or "")
        if "escape" in msg or "required" in msg or "excluded" in msg:
            return web.json_response(payload, status=400)
        return web.json_response(payload, status=404)
    return web.json_response(payload)


async def handle_skill_toggle(request: web.Request) -> web.Response:
    """POST /hermes/skills/toggle — body ``{name, enabled}``.

    Mirrors upstream ``PUT /api/skills/toggle`` in semantics; we expose it
    as POST to keep our HTTP verb conventions consistent across the
    ``/hermes/*`` surface.
    """
    try:
        body = await read_json_object(request)
    except web.HTTPBadRequest as exc:
        return exc
    name = body.get("name")
    enabled = body.get("enabled")
    if not isinstance(name, str) or not name.strip():
        return json_error(400, "name is required")
    if not isinstance(enabled, bool):
        return json_error(400, "enabled must be a boolean")
    try:
        payload = toggle_skill(name, enabled)
    except OSError as exc:
        return json_error(500, str(exc))
    if not payload.get("ok"):
        return web.json_response(payload, status=400)
    return web.json_response(payload)


def register_skills_routes(app: web.Application) -> None:
    # `name` is URL-encoded by the client; aiohttp's `{name}` matcher already
    # decodes percent-escapes for us. Skill names with slashes don't exist
    # in practice (the discovery walk treats path segments as the category
    # boundary), so the single-segment matcher is safe.
    app.add_routes(
        [
            web.get("/hermes/skills", handle_skills_list),
            web.get("/hermes/skills/{name}/files", handle_skill_files),
            web.get("/hermes/skills/{name}/file", handle_skill_file),
            web.post("/hermes/skills/toggle", handle_skill_toggle),
        ]
    )
