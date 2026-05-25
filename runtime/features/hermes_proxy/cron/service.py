"""Thin wrapper around Hermes Agent's ``cron.jobs`` module.

Hermes already owns cron job storage, scheduling, and the per-job lifecycle
(``$HERMES_HOME/cron/jobs.json``, the in-process file lock, schedule parsing,
next-run computation). The bridge's only job here is to expose the same CRUD
surface over HTTP so the options page can manage jobs without duplicating
any of that logic.

Mirrors what the upstream FastAPI server exposes at ``/api/cron/jobs`` —
see ``hermes_cli/web_server.py:2563`` and onwards — but uses the underlying
``cron.jobs`` Python functions directly, the same way ``skills_service`` and
``memory_service`` reach past the upstream HTTP layer.

Note on cron output visibility: every cron run is automatically
discoverable via the new-tab page, regardless of the ``deliver`` setting.
Hermes core always writes the run's markdown to
``$HERMES_HOME/cron/output/{job_id}/*.md``; the bridge serves that
index through ``output_service``. ``deliver`` only controls *additional*
channel push (Feishu / Telegram / etc.) on top of that. The frontend
may pass ``deliver="inbox"`` as a legacy alias for ``"local"``; both
mean "file-only, don't push to any messaging channel".

Note on the legacy "Hermes Card" injection: an earlier version of this
module appended a ``## Hermes Card`` instruction block to every cron
prompt so the extension could render scannable cards. That coupling was
a layering mistake — cron creation should not know about the extension's
display surface. The injection is gone; the extension renders cron output
verbatim. A one-shot migration (`_migrate_strip_legacy_protocol`) removes
the marker block from any prompts that were already augmented.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from ....adapters.hermes_core import hermes_home

logger = logging.getLogger("my-browser-bridge")


# ---------------------------------------------------------------------------
# Upstream-backed helpers — defensive imports so the bridge can still return
# a structured "cron unavailable" error when running outside a Hermes install.
# ---------------------------------------------------------------------------


def _cron_unavailable_error(exc: Exception) -> str:
    return (
        "Hermes cron module not importable (is the bridge running inside "
        f"Hermes Agent's venv?): {exc}"
    )


# ---------------------------------------------------------------------------
# Legacy "Inbox protocol" cleanup — strip the marker block from prompts that
# the previous version of this module augmented. The marker is a hidden HTML
# comment we used as an idempotency anchor; the block runs from the marker
# to end-of-prompt (the instruction was always appended at the tail).
# ---------------------------------------------------------------------------


_LEGACY_INBOX_PROTOCOL_MARKER = "<!-- hermes-inbox-protocol-v1 -->"
# Strip the marker + everything that followed it (the protocol body lived at
# the tail of the prompt). We also chew up the blank line(s) that separated
# the original prompt from the appended block so the cleaned text doesn't
# end with stray whitespace.
_LEGACY_INBOX_PROTOCOL_RE = re.compile(
    r"\n*\s*" + re.escape(_LEGACY_INBOX_PROTOCOL_MARKER) + r".*\Z",
    re.DOTALL,
)


def _strip_legacy_inbox_protocol(prompt: Any) -> Any:
    """Remove the legacy ``<!-- hermes-inbox-protocol-v1 -->`` block.

    Returns ``prompt`` unchanged for non-strings or prompts without the
    marker. Used on read (so the options page never shows the stale
    instructions) and on update (so an edit re-saves a clean prompt).
    """
    if not isinstance(prompt, str):
        return prompt
    if _LEGACY_INBOX_PROTOCOL_MARKER not in prompt:
        return prompt
    return _LEGACY_INBOX_PROTOCOL_RE.sub("", prompt).rstrip() + "\n"


# Legacy deliver alias: "inbox" → "local". An earlier frontend used
# "inbox" to mean "file-only, no channel push"; we keep accepting it
# (and normalise it back to the canonical "local") so saved configs
# from that era still round-trip cleanly.
def _normalise_deliver(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() == "inbox":
        return "local"
    return value


_MIGRATION_FLAG_PATH_PARTS = ("inbox", "legacy-protocol-stripped.marker")


def _migration_flag_path():
    return hermes_home().joinpath(*_MIGRATION_FLAG_PATH_PARTS)


_migration_attempted = False


def _migrate_strip_legacy_protocol() -> None:
    """Walk every cron job once and re-save prompts with the legacy block
    stripped. Idempotent via a marker file under ``$HERMES_HOME/inbox/``.

    Runs lazily — gated by the first call to a public CRUD function — so a
    bridge that never touches cron jobs doesn't pay the import cost.
    Failures are logged but never raise; this is best-effort cleanup, not
    a correctness path.
    """
    global _migration_attempted
    if _migration_attempted:
        return
    _migration_attempted = True

    flag = _migration_flag_path()
    try:
        if flag.exists():
            return
    except OSError:
        return

    try:
        from cron.jobs import list_jobs, update_job  # type: ignore
    except Exception as exc:
        logger.debug(
            "legacy inbox-protocol migration skipped (cron module unavailable): %s",
            exc,
        )
        return

    try:
        jobs = list_jobs(include_disabled=True)
    except Exception as exc:
        logger.warning("legacy inbox-protocol migration: list_jobs failed: %s", exc)
        return

    cleaned = 0
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        prompt = job.get("prompt")
        if not isinstance(prompt, str):
            continue
        if _LEGACY_INBOX_PROTOCOL_MARKER not in prompt:
            continue
        stripped = _strip_legacy_inbox_protocol(prompt)
        if stripped == prompt:
            continue
        job_id = job.get("id")
        if not isinstance(job_id, str) or not job_id:
            continue
        try:
            update_job(job_id, {"prompt": stripped})
            cleaned += 1
        except Exception as exc:
            logger.warning(
                "legacy inbox-protocol migration: update_job %s failed: %s",
                job_id,
                exc,
            )

    if cleaned:
        logger.info(
            "stripped legacy inbox-protocol block from %d cron job(s)", cleaned
        )

    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("ok\n", encoding="utf-8")
    except OSError as exc:
        # Migration ran but we couldn't persist the flag — next process
        # start will re-run it. Idempotent, so this is merely wasteful,
        # not incorrect.
        logger.debug("could not write migration flag %s: %s", flag, exc)


def _clean_job(job: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Strip the legacy protocol block from a job's ``prompt`` field
    before returning it to the frontend.

    Defensive: in case the disk-side migration hasn't run yet (e.g. it
    failed earlier or the flag file got removed), reads still surface
    clean prompts.
    """
    if not isinstance(job, dict):
        return job
    prompt = job.get("prompt")
    if isinstance(prompt, str) and _LEGACY_INBOX_PROTOCOL_MARKER in prompt:
        job = dict(job)
        job["prompt"] = _strip_legacy_inbox_protocol(prompt)
    return job


def list_jobs_response() -> Dict[str, Any]:
    """Return all cron jobs, including paused/disabled ones."""
    _migrate_strip_legacy_protocol()
    try:
        from cron.jobs import list_jobs  # type: ignore
    except Exception as exc:
        logger.warning("cron.jobs.list_jobs unavailable: %s", exc)
        return {"ok": False, "error": _cron_unavailable_error(exc), "jobs": []}
    try:
        jobs = list_jobs(include_disabled=True)
    except Exception as exc:
        logger.exception("list_jobs failed")
        return {"ok": False, "error": f"list_jobs failed: {exc}", "jobs": []}
    return {"ok": True, "jobs": [_clean_job(j) for j in (jobs or [])]}


def get_job_response(job_id: str) -> Dict[str, Any]:
    _migrate_strip_legacy_protocol()
    try:
        from cron.jobs import get_job  # type: ignore
    except Exception as exc:
        return {"ok": False, "error": _cron_unavailable_error(exc)}
    try:
        job = get_job(job_id)
    except Exception as exc:
        logger.exception("get_job %s failed", job_id)
        return {"ok": False, "error": f"get_job failed: {exc}"}
    if not job:
        return {"ok": False, "error": "job not found"}
    return {"ok": True, "job": _clean_job(job)}


def create_job_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new cron job.

    Accepts a subset of ``cron.jobs.create_job`` kwargs that make sense from
    the options page: ``prompt`` / ``schedule`` / ``name`` / ``deliver`` /
    ``repeat`` / ``skills`` / ``model`` / ``provider`` / ``base_url`` /
    ``script`` / ``no_agent`` / ``context_from`` / ``enabled_toolsets`` /
    ``workdir``. Extra keys are ignored so the frontend can stay forward-
    compatible without breaking when upstream adds new ones.
    """
    _migrate_strip_legacy_protocol()
    try:
        from cron.jobs import create_job  # type: ignore
    except Exception as exc:
        return {"ok": False, "error": _cron_unavailable_error(exc)}

    schedule = payload.get("schedule")
    if not isinstance(schedule, str) or not schedule.strip():
        return {"ok": False, "error": "schedule is required"}

    prompt = payload.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        return {"ok": False, "error": "prompt must be a string"}

    no_agent = bool(payload.get("no_agent", False))
    script = payload.get("script")
    if script is not None and not isinstance(script, str):
        return {"ok": False, "error": "script must be a string"}

    if not no_agent and not (isinstance(prompt, str) and prompt.strip()):
        return {"ok": False, "error": "prompt is required unless no_agent=true"}

    # Defensive strip on create too — a frontend that round-trips an older
    # job's prompt (edit-as-new) shouldn't accidentally re-introduce the
    # marker block.
    if isinstance(prompt, str):
        prompt = _strip_legacy_inbox_protocol(prompt)

    kwargs: Dict[str, Any] = {
        "prompt": prompt,
        "schedule": schedule.strip(),
        "no_agent": no_agent,
    }

    for key in (
        "name",
        "deliver",
        "model",
        "provider",
        "base_url",
        "script",
        "workdir",
    ):
        if key in payload and payload[key] is not None:
            kwargs[key] = payload[key]
    if "deliver" in kwargs:
        kwargs["deliver"] = _normalise_deliver(kwargs["deliver"])

    repeat = payload.get("repeat")
    if isinstance(repeat, int):
        kwargs["repeat"] = repeat
    elif isinstance(repeat, dict) and isinstance(repeat.get("times"), int):
        kwargs["repeat"] = repeat["times"]

    if isinstance(payload.get("skills"), list):
        kwargs["skills"] = [str(s) for s in payload["skills"] if str(s).strip()]
    elif isinstance(payload.get("skill"), str) and payload["skill"].strip():
        kwargs["skill"] = payload["skill"].strip()

    if isinstance(payload.get("enabled_toolsets"), list):
        kwargs["enabled_toolsets"] = [
            str(t) for t in payload["enabled_toolsets"] if str(t).strip()
        ]

    if isinstance(payload.get("context_from"), (list, str)):
        kwargs["context_from"] = payload["context_from"]

    try:
        job = create_job(**kwargs)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("create_job failed")
        return {"ok": False, "error": f"create_job failed: {exc}"}
    return {"ok": True, "job": _clean_job(job)}


# Fields the frontend is allowed to pass through to ``cron.jobs.update_job``.
# Locked down so a stray field name can't silently mutate scheduler-internal
# state (``state``, ``last_run_at``, ``next_run_at``, ...).
_UPDATABLE_FIELDS = frozenset(
    (
        "name",
        "prompt",
        "schedule",
        "deliver",
        "skills",
        "skill",
        "model",
        "provider",
        "base_url",
        "script",
        "no_agent",
        "context_from",
        "enabled_toolsets",
        "workdir",
        "repeat",
    )
)


def update_job_response(job_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    _migrate_strip_legacy_protocol()
    try:
        from cron.jobs import update_job  # type: ignore
    except Exception as exc:
        return {"ok": False, "error": _cron_unavailable_error(exc)}

    filtered: Dict[str, Any] = {
        k: v for k, v in updates.items() if k in _UPDATABLE_FIELDS
    }
    if not filtered:
        return {"ok": False, "error": "no updatable fields supplied"}

    # Strip the legacy block on update too — the user editing a prompt
    # from the options page should never have to scroll past stale
    # injected instructions to find their actual text.
    if isinstance(filtered.get("prompt"), str):
        filtered["prompt"] = _strip_legacy_inbox_protocol(filtered["prompt"])

    if "deliver" in filtered:
        filtered["deliver"] = _normalise_deliver(filtered["deliver"])

    try:
        job = update_job(job_id, filtered)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("update_job %s failed", job_id)
        return {"ok": False, "error": f"update_job failed: {exc}"}
    if not job:
        return {"ok": False, "error": "job not found"}
    return {"ok": True, "job": _clean_job(job)}


def _lifecycle_op(job_id: str, op_name: str) -> Dict[str, Any]:
    try:
        if op_name == "pause":
            from cron.jobs import pause_job as _op  # type: ignore
        elif op_name == "resume":
            from cron.jobs import resume_job as _op  # type: ignore
        elif op_name == "trigger":
            from cron.jobs import trigger_job as _op  # type: ignore
        else:
            return {"ok": False, "error": f"unknown op: {op_name}"}
    except Exception as exc:
        return {"ok": False, "error": _cron_unavailable_error(exc)}
    try:
        job = _op(job_id)
    except Exception as exc:
        logger.exception("%s %s failed", op_name, job_id)
        return {"ok": False, "error": f"{op_name} failed: {exc}"}
    if not job:
        return {"ok": False, "error": "job not found"}
    return {"ok": True, "job": _clean_job(job)}


def pause_job_response(job_id: str) -> Dict[str, Any]:
    return _lifecycle_op(job_id, "pause")


def resume_job_response(job_id: str) -> Dict[str, Any]:
    return _lifecycle_op(job_id, "resume")


def trigger_job_response(job_id: str) -> Dict[str, Any]:
    return _lifecycle_op(job_id, "trigger")


def delete_job_response(job_id: str) -> Dict[str, Any]:
    try:
        from cron.jobs import remove_job  # type: ignore
    except Exception as exc:
        return {"ok": False, "error": _cron_unavailable_error(exc)}
    try:
        removed = remove_job(job_id)
    except Exception as exc:
        logger.exception("remove_job %s failed", job_id)
        return {"ok": False, "error": f"remove_job failed: {exc}"}
    if not removed:
        return {"ok": False, "error": "job not found"}
    return {"ok": True}


def parse_schedule_preview(schedule: str) -> Dict[str, Any]:
    """Preview how Hermes will parse a schedule string.

    Lets the options page give immediate feedback on invalid schedules
    without round-tripping a job create. Mirrors ``cron.jobs.parse_schedule``
    plus ``compute_next_run`` so the UI can also display the next run.
    """
    if not isinstance(schedule, str) or not schedule.strip():
        return {"ok": False, "error": "schedule is required"}
    try:
        from cron.jobs import compute_next_run, parse_schedule  # type: ignore
    except Exception as exc:
        return {"ok": False, "error": _cron_unavailable_error(exc)}
    try:
        parsed = parse_schedule(schedule.strip())
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("parse_schedule failed")
        return {"ok": False, "error": f"parse_schedule failed: {exc}"}
    try:
        next_run = compute_next_run(parsed)
    except Exception as exc:
        logger.warning("compute_next_run failed: %s", exc)
        next_run = None
    return {
        "ok": True,
        "schedule": parsed,
        "display": parsed.get("display"),
        "next_run_at": next_run,
    }
