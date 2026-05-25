"""HTTP routes for ``/hermes/sessions/*`` — Hermes session log over HTTP.

Read:

- ``GET    /hermes/sessions`` — paginated list (mirrors ``GET /api/sessions``)
- ``GET    /hermes/sessions/{session_id}`` — single session metadata
- ``GET    /hermes/sessions/{session_id}/messages`` — full ordered message log

Write:

- ``POST   /hermes/sessions`` — create a new session row
- ``POST   /hermes/sessions/{session_id}/messages`` — append one message
- ``PATCH  /hermes/sessions/{session_id}`` — mutate session metadata (title)
- ``DELETE /hermes/sessions/{session_id}`` — delete session + its messages

Read endpoints match the dashboard's FastAPI app (``hermes_cli/web_server.py``)
so a client written against ``/api/sessions/*`` can swap base URLs with no
schema diff. Write endpoints have no dashboard equivalent; their shapes are
defined here. Service-level errors (``hermes_state`` unreachable, DB locked
past retry budget) come back as ``{"ok": false, "error": "..."}`` with 503;
not-found is 404; title conflicts are 409; validation errors are 400.
"""

from __future__ import annotations

from aiohttp import web

from ....common import read_json_object
from .service import (
    MAX_MESSAGE_BODY_BYTES,
    append_message_response,
    create_session_response,
    delete_session_response,
    get_messages_response,
    get_session_response,
    list_sessions_response,
    regenerate_title_response,
    update_session_response,
)


def _parse_int(value: str | None, default: int) -> int:
    """Coerce a query-string int, falling back to *default* on bad input.

    Negative values are clamped to 0 — list_sessions_rich would just
    return nothing on a negative offset, so a clean clamp gives callers
    a more predictable surface than echoing the bad value.
    """
    if value is None or value == "":
        return default
    try:
        n = int(value)
    except ValueError:
        return default
    return max(0, n)


_DEFAULT_LIMIT = 20
_MAX_LIMIT = 200  # mirrors common pagination ceiling; SessionDB has no hard cap


async def handle_list_sessions(request: web.Request) -> web.Response:
    limit = min(_parse_int(request.query.get("limit"), _DEFAULT_LIMIT), _MAX_LIMIT)
    offset = _parse_int(request.query.get("offset"), 0)
    # Filtering: ``source=cli`` keeps only sessions with that source;
    # ``exclude_sources=cron,api_server`` (comma-separated OR repeated)
    # drops sessions with any matching source. Both pass straight through
    # to SessionDB.list_sessions_rich.
    source = request.query.get("source") or None
    raw_excludes = request.query.getall("exclude_sources", [])
    exclude_sources: list[str] = []
    for raw in raw_excludes:
        for piece in raw.split(","):
            piece = piece.strip()
            if piece:
                exclude_sources.append(piece)
    payload = list_sessions_response(
        limit=limit,
        offset=offset,
        source=source,
        exclude_sources=exclude_sources or None,
    )
    if not payload.get("ok"):
        # Service couldn't even reach SessionDB — surface as 503 so a
        # client can distinguish "Hermes isn't ready yet" from "session
        # missing" (which is 404 below).
        return web.json_response(payload, status=503)
    return web.json_response(payload)


async def handle_get_session(request: web.Request) -> web.Response:
    session_id = request.match_info.get("session_id", "")
    payload = get_session_response(session_id)
    if not payload.get("ok"):
        status = 404 if payload.get("error") == "session not found" else 503
        return web.json_response(payload, status=status)
    return web.json_response(payload)


async def handle_get_messages(request: web.Request) -> web.Response:
    session_id = request.match_info.get("session_id", "")
    payload = get_messages_response(session_id)
    if not payload.get("ok"):
        status = 404 if payload.get("error") == "session not found" else 503
        return web.json_response(payload, status=status)
    return web.json_response(payload)


# ---------------------------------------------------------------------------
# Write handlers
# ---------------------------------------------------------------------------


# Distinguishes "validation says no" (400) from "Hermes core says no" (503).
# Validation errors come back from the service with a deterministic prefix
# we set ourselves; anything else from the service is treated as upstream
# unavailability.
_VALIDATION_PREFIXES = (
    "id must be a string",
    "role is required",
    "title must be a string",
)


def _is_validation_error(msg: str) -> bool:
    return any(msg.startswith(p) for p in _VALIDATION_PREFIXES)


async def handle_create_session(request: web.Request) -> web.Response:
    try:
        body = await read_json_object(request)
    except web.HTTPBadRequest as exc:
        return exc
    payload = create_session_response(body)
    if not payload.get("ok"):
        status = 400 if _is_validation_error(payload.get("error", "")) else 503
        return web.json_response(payload, status=status)
    return web.json_response(payload, status=201)


async def handle_append_message(request: web.Request) -> web.Response:
    session_id = request.match_info.get("session_id", "")
    try:
        body = await read_json_object(request, max_bytes=MAX_MESSAGE_BODY_BYTES)
    except web.HTTPBadRequest as exc:
        return exc
    payload = append_message_response(session_id, body)
    if not payload.get("ok"):
        err = payload.get("error", "")
        if err == "session not found":
            status = 404
        elif _is_validation_error(err):
            status = 400
        else:
            status = 503
        return web.json_response(payload, status=status)
    return web.json_response(payload, status=201)


async def handle_update_session(request: web.Request) -> web.Response:
    session_id = request.match_info.get("session_id", "")
    try:
        body = await read_json_object(request)
    except web.HTTPBadRequest as exc:
        return exc
    payload = update_session_response(session_id, body)
    if not payload.get("ok"):
        kind = payload.get("kind")
        err = payload.get("error", "")
        if err == "session not found":
            status = 404
        elif kind == "title_conflict":
            status = 409
        elif kind == "invalid_title" or _is_validation_error(err):
            status = 400
        else:
            status = 503
        return web.json_response(payload, status=status)
    return web.json_response(payload)


async def handle_delete_session(request: web.Request) -> web.Response:
    session_id = request.match_info.get("session_id", "")
    payload = delete_session_response(session_id)
    if not payload.get("ok"):
        status = 404 if payload.get("error") == "session not found" else 503
        return web.json_response(payload, status=status)
    return web.json_response(payload)


async def handle_regenerate_title(request: web.Request) -> web.Response:
    """POST /hermes/sessions/{id}/regenerate-title.

    Forces a title generation regardless of the "first two user messages"
    window that the auto-title path respects. Synchronous LLM call —
    expect 5-10s on warm-cache providers, longer when the auxiliary
    chain has to fall back.
    """
    session_id = request.match_info.get("session_id", "")
    payload = regenerate_title_response(session_id)
    if not payload.get("ok"):
        err = payload.get("error", "")
        if err == "session not found":
            status = 404
        elif payload.get("kind") == "title_conflict":
            status = 409
        elif (
            err.startswith("session has no messages")
            or err.startswith("session does not have a complete")
        ):
            status = 400
        else:
            status = 503
        return web.json_response(payload, status=status)
    return web.json_response(payload)


def register(app: web.Application) -> None:
    app.add_routes(
        [
            web.get("/hermes/sessions", handle_list_sessions),
            web.post("/hermes/sessions", handle_create_session),
            web.get("/hermes/sessions/{session_id}", handle_get_session),
            web.patch("/hermes/sessions/{session_id}", handle_update_session),
            web.delete("/hermes/sessions/{session_id}", handle_delete_session),
            web.get(
                "/hermes/sessions/{session_id}/messages", handle_get_messages
            ),
            web.post(
                "/hermes/sessions/{session_id}/messages", handle_append_message
            ),
            web.post(
                "/hermes/sessions/{session_id}/regenerate-title",
                handle_regenerate_title,
            ),
        ]
    )
