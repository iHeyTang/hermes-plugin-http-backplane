from __future__ import annotations

from aiohttp import web

from ....common import json_error, read_json_object
from .output_service import get_run, list_recent_runs
from .service import (
    create_job_response,
    delete_job_response,
    get_job_response,
    list_jobs_response,
    parse_schedule_preview,
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


async def handle_list_jobs(_request: web.Request) -> web.Response:
    return web.json_response(list_jobs_response())


async def handle_get_job(request: web.Request) -> web.Response:
    job_id = request.match_info.get("job_id", "")
    payload = get_job_response(job_id)
    if not payload.get("ok"):
        status = 404 if payload.get("error") == "job not found" else 500
        return web.json_response(payload, status=status)
    return web.json_response(payload)


async def handle_create_job(request: web.Request) -> web.Response:
    try:
        body = await read_json_object(request)
    except web.HTTPBadRequest as exc:
        return exc
    payload = create_job_response(body)
    if not payload.get("ok"):
        return web.json_response(payload, status=400)
    return web.json_response(payload)


async def handle_update_job(request: web.Request) -> web.Response:
    job_id = request.match_info.get("job_id", "")
    try:
        body = await read_json_object(request)
    except web.HTTPBadRequest as exc:
        return exc
    # Accept either ``{ "updates": {...} }`` (mirrors upstream's
    # ``CronJobUpdate`` shape) or a flat object — flat is easier for the
    # frontend's optimistic-update path, and "updates" stays as an escape
    # hatch for future fields with names that clash with envelope keys.
    updates = body.get("updates") if isinstance(body.get("updates"), dict) else body
    payload = update_job_response(job_id, updates)
    if not payload.get("ok"):
        status = 404 if payload.get("error") == "job not found" else 400
        return web.json_response(payload, status=status)
    return web.json_response(payload)


async def handle_pause_job(request: web.Request) -> web.Response:
    job_id = request.match_info.get("job_id", "")
    payload = pause_job_response(job_id)
    if not payload.get("ok"):
        status = 404 if payload.get("error") == "job not found" else 500
        return web.json_response(payload, status=status)
    return web.json_response(payload)


async def handle_resume_job(request: web.Request) -> web.Response:
    job_id = request.match_info.get("job_id", "")
    payload = resume_job_response(job_id)
    if not payload.get("ok"):
        status = 404 if payload.get("error") == "job not found" else 500
        return web.json_response(payload, status=status)
    return web.json_response(payload)


async def handle_trigger_job(request: web.Request) -> web.Response:
    job_id = request.match_info.get("job_id", "")
    payload = trigger_job_response(job_id)
    if not payload.get("ok"):
        status = 404 if payload.get("error") == "job not found" else 500
        return web.json_response(payload, status=status)
    return web.json_response(payload)


async def handle_delete_job(request: web.Request) -> web.Response:
    job_id = request.match_info.get("job_id", "")
    payload = delete_job_response(job_id)
    if not payload.get("ok"):
        status = 404 if payload.get("error") == "job not found" else 500
        return web.json_response(payload, status=status)
    return web.json_response(payload)


async def handle_output_index(request: web.Request) -> web.Response:
    since_ms = _parse_int(request.query.get("since_ms"))
    limit = _parse_int(request.query.get("limit"), 100) or 100
    include_silent = request.query.get("include_silent", "1") not in ("0", "false", "no")
    payload = list_recent_runs(
        since_ms=since_ms,
        limit=limit,
        include_silent=include_silent,
    )
    return web.json_response(payload)


async def handle_output_detail(request: web.Request) -> web.Response:
    job_id = request.match_info.get("job_id", "")
    run_id = request.match_info.get("run_id", "")
    payload = get_run(job_id, run_id)
    if not payload.get("ok"):
        status = 404 if payload.get("error") == "run not found" else 400
        return web.json_response(payload, status=status)
    return web.json_response(payload)


async def handle_parse_schedule(request: web.Request) -> web.Response:
    try:
        body = await read_json_object(request)
    except web.HTTPBadRequest as exc:
        return exc
    schedule = body.get("schedule")
    if not isinstance(schedule, str):
        return json_error(400, "schedule must be a string")
    payload = parse_schedule_preview(schedule)
    if not payload.get("ok"):
        return web.json_response(payload, status=400)
    return web.json_response(payload)


def register(app: web.Application) -> None:
    app.add_routes(
        [
            web.get("/hermes/cron/jobs", handle_list_jobs),
            web.post("/hermes/cron/jobs", handle_create_job),
            web.post("/hermes/cron/parse-schedule", handle_parse_schedule),
            web.get("/hermes/cron/jobs/{job_id}", handle_get_job),
            web.post("/hermes/cron/jobs/{job_id}", handle_update_job),
            web.delete("/hermes/cron/jobs/{job_id}", handle_delete_job),
            web.post("/hermes/cron/jobs/{job_id}/pause", handle_pause_job),
            web.post("/hermes/cron/jobs/{job_id}/resume", handle_resume_job),
            web.post("/hermes/cron/jobs/{job_id}/trigger", handle_trigger_job),
            web.get("/hermes/cron/output/index", handle_output_index),
            web.get(
                "/hermes/cron/output/{job_id}/{run_id}", handle_output_detail
            ),
        ]
    )
