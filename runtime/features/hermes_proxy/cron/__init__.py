"""Cron — HTTP wrapper around Hermes core's ``cron.jobs`` module plus a
read-only index over the markdown files it writes for each run.

Hermes core already owns cron job storage, scheduling, and lifecycle; we
only expose its API over HTTP. The ``output_service`` reads cron run
files off disk and serves them as JSON — that's the only data the new-tab
page needs.

Standalone-extractable: depends only on Hermes core's ``cron.jobs`` API
and ``$HERMES_HOME``.
"""

from __future__ import annotations

from .output_service import get_run, list_recent_runs
from .routes import register
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

__all__ = [
    "register",
    "list_jobs_response",
    "get_job_response",
    "create_job_response",
    "update_job_response",
    "delete_job_response",
    "pause_job_response",
    "resume_job_response",
    "trigger_job_response",
    "parse_schedule_preview",
    "get_run",
    "list_recent_runs",
]
