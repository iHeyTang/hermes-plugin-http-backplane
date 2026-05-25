from __future__ import annotations

from aiohttp import web

from .skills_service import (
    list_skill_files,
    list_skills_response,
    read_skill_file,
    toggle_skill,
)
from ....common import json_error, read_json_object


_UPSTREAM_SKILL_FIELDS = ("name", "description", "category", "enabled")
_RICH_SKILL_FIELDS = (
    "name",
    "path",
    "origin",
    "platforms",
    "version",
    "tags",
    "created_at",
    "updated_at",
    "timestamp_source",
)


async def handle_skills_list(_request: web.Request) -> web.Response:
    """GET /hermes/skills — strict 1:1 with upstream GET /api/skills.

    Each entry carries only ``{name, description, category, enabled}``
    (matches what ``tools.skills_tool._find_all_skills`` returns +
    ``enabled`` injected by upstream's route).

    All the rich per-skill metadata the backplane also computes
    (``path``, ``origin``, ``platforms``, ``version``, ``tags``,
    timestamps, source) lives on the mine-only ``GET /hermes/skills/meta``
    so UI clients can join by name when they want it.
    """
    try:
        payload = list_skills_response()
    except OSError as exc:
        return json_error(500, str(exc))
    skills = payload.get("skills") or []
    return web.json_response(
        [
            {k: s.get(k) for k in _UPSTREAM_SKILL_FIELDS if k in s}
            for s in skills
            if isinstance(s, dict)
        ]
    )


async def handle_skills_meta(_request: web.Request) -> web.Response:
    """GET /hermes/skills/meta — mine-only metadata sidecar for /hermes/skills.

    Returns::

        {
          "skills_dirs": [...],
          "totals":      {"total": N, "enabled": N, "disabled": N},
          "origin_counts": {origin: count, ...},
          "items": [
            {name, path, origin, platforms, version, tags,
             created_at, updated_at, timestamp_source},
            ...
          ]
        }

    ``items`` is keyed alongside ``/hermes/skills`` by ``name``; UI
    clients fetch both endpoints in parallel and join per-entry on the
    rich metadata they need. Bundle-level diagnostics (``skills_dirs``,
    ``totals``, ``origin_counts``) come from the same disk scan so
    they're cheap to bundle here.
    """
    try:
        payload = list_skills_response()
    except OSError as exc:
        return json_error(500, str(exc))
    skills = payload.get("skills") or []
    return web.json_response(
        {
            "skills_dirs": payload.get("skills_dirs") or [],
            "totals": payload.get("totals") or {},
            "origin_counts": payload.get("origin_counts") or {},
            "items": [
                {k: s.get(k) for k in _RICH_SKILL_FIELDS if k in s}
                for s in skills
                if isinstance(s, dict)
            ],
        }
    )


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
    """PUT /hermes/skills/toggle — body ``{name, enabled}``.

    Method + body shape mirror upstream ``PUT /api/skills/toggle``.
    Response shape matches upstream: ``{ok, name, enabled}``.
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
            # Mine-only companion that exposes the bundle-level scan
            # diagnostics that don't fit a raw list response.
            web.get("/hermes/skills/meta", handle_skills_meta),
            web.get("/hermes/skills/{name}/files", handle_skill_files),
            web.get("/hermes/skills/{name}/file", handle_skill_file),
            # PUT mirrors upstream PUT /api/skills/toggle.
            web.put("/hermes/skills/toggle", handle_skill_toggle),
        ]
    )
