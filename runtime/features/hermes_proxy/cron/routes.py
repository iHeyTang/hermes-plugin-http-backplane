from __future__ import annotations

from aiohttp import web

from ....common import json_error, read_json_object
from .output_service import list_recent_runs
from .service import (
    create_job_response,
    delete_job_response,
    get_job_response,
    list_jobs_response,
    pause_job_response,
    resume_job_response,
    trigger_job_response,
    update_job_response,
)


def _parse_int(value: str | None, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _profile_query(request: web.Request) -> str | None:
    """Pull ``?profile=`` if set. None → endpoint-specific default."""
    p = request.query.get("profile")
    return p if p else None


async def handle_list_jobs(request: web.Request) -> web.Response:
    profile = _profile_query(request) or "all"
    payload = list_jobs_response(profile)
    if not payload.get("ok"):
        status = 404 if "does not exist" in (payload.get("error") or "") else 500
        return web.json_response(payload, status=status)
    return web.json_response(payload.get("jobs", []))


async def handle_get_job(request: web.Request) -> web.Response:
    job_id = request.match_info.get("job_id", "")
    payload = get_job_response(job_id, _profile_query(request))
    if not payload.get("ok"):
        status = 404 if payload.get("error") in ("job not found",) or "does not exist" in (payload.get("error") or "") else 500
        return web.json_response(payload, status=status)
    return web.json_response(payload["job"])


async def handle_create_job(request: web.Request) -> web.Response:
    try:
        body = await read_json_object(request)
    except web.HTTPBadRequest as exc:
        return exc
    profile = _profile_query(request) or "default"
    payload = create_job_response(body, profile)
    if not payload.get("ok"):
        status = 404 if "does not exist" in (payload.get("error") or "") else 400
        return web.json_response(payload, status=status)
    return web.json_response(payload["job"])


async def handle_update_job(request: web.Request) -> web.Response:
    """PUT /hermes/cron/jobs/{id} — strict upstream shape ``{ "updates": {...} }``."""
    job_id = request.match_info.get("job_id", "")
    try:
        body = await read_json_object(request)
    except web.HTTPBadRequest as exc:
        return exc
    updates = body.get("updates")
    if not isinstance(updates, dict):
        return json_error(400, 'body must be {"updates": {...}}')
    payload = update_job_response(job_id, updates, _profile_query(request))
    if not payload.get("ok"):
        status = 404 if payload.get("error") in ("job not found",) or "does not exist" in (payload.get("error") or "") else 400
        return web.json_response(payload, status=status)
    return web.json_response(payload["job"])


async def handle_pause_job(request: web.Request) -> web.Response:
    job_id = request.match_info.get("job_id", "")
    payload = pause_job_response(job_id, _profile_query(request))
    if not payload.get("ok"):
        status = 404 if payload.get("error") in ("job not found",) or "does not exist" in (payload.get("error") or "") else 500
        return web.json_response(payload, status=status)
    return web.json_response(payload["job"])


async def handle_resume_job(request: web.Request) -> web.Response:
    job_id = request.match_info.get("job_id", "")
    payload = resume_job_response(job_id, _profile_query(request))
    if not payload.get("ok"):
        status = 404 if payload.get("error") in ("job not found",) or "does not exist" in (payload.get("error") or "") else 500
        return web.json_response(payload, status=status)
    return web.json_response(payload["job"])


async def handle_trigger_job(request: web.Request) -> web.Response:
    job_id = request.match_info.get("job_id", "")
    payload = trigger_job_response(job_id, _profile_query(request))
    if not payload.get("ok"):
        status = 404 if payload.get("error") in ("job not found",) or "does not exist" in (payload.get("error") or "") else 500
        return web.json_response(payload, status=status)
    return web.json_response(payload["job"])


async def handle_delete_job(request: web.Request) -> web.Response:
    job_id = request.match_info.get("job_id", "")
    payload = delete_job_response(job_id, _profile_query(request))
    if not payload.get("ok"):
        status = 404 if payload.get("error") in ("job not found",) or "does not exist" in (payload.get("error") or "") else 500
        return web.json_response(payload, status=status)
    # Upstream returns {ok: true}.
    return web.json_response(payload)


async def handle_list_runs(request: web.Request) -> web.Response:
    """GET /hermes/cron/runs — list recent cron run outputs.

    Each entry has parsed metadata + body of the markdown file Hermes
    writes to ``$HERMES_HOME/cron/output/{job_id}/<timestamp>.md`` per
    execution. Query params: ``since_ms``, ``limit`` (default 100),
    ``include_silent``.
    """
    since_ms = _parse_int(request.query.get("since_ms"))
    limit = _parse_int(request.query.get("limit"), 100) or 100
    include_silent = request.query.get("include_silent", "1") not in ("0", "false", "no")
    payload = list_recent_runs(
        since_ms=since_ms,
        limit=limit,
        include_silent=include_silent,
    )
    return web.json_response(payload)


def register(app: web.Application) -> None:
    app.add_routes(
        [
            web.get("/hermes/cron/jobs", handle_list_jobs),
            web.post("/hermes/cron/jobs", handle_create_job),
            web.get("/hermes/cron/jobs/{job_id}", handle_get_job),
            # PUT mirrors upstream PUT /api/cron/jobs/{id}.
            web.put("/hermes/cron/jobs/{job_id}", handle_update_job),
            web.delete("/hermes/cron/jobs/{job_id}", handle_delete_job),
            web.post("/hermes/cron/jobs/{job_id}/pause", handle_pause_job),
            web.post("/hermes/cron/jobs/{job_id}/resume", handle_resume_job),
            web.post("/hermes/cron/jobs/{job_id}/trigger", handle_trigger_job),
            web.get("/hermes/cron/runs", handle_list_runs),
        ]
    )
