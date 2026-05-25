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
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ....adapters.hermes_core import hermes_home

logger = logging.getLogger("my-browser-bridge")


# ---------------------------------------------------------------------------
# Cross-profile dispatch — mirrors upstream `_call_cron_for_profile` so that
# `?profile=<name>` / `?profile=all` work the same way they do against
# `hermes_cli/web_server.py`.
#
# Risk note: the swap mutates module-level globals on `cron.jobs`. The lock
# below serialises this against other backplane handlers; if the same
# process also runs the cron scheduler (`hermes cron run`), the scheduler
# does not take this lock and could in theory observe a swapped path
# mid-call. We accept the risk because upstream does the same thing and
# the swap window is microseconds. If this ever becomes a real problem,
# the alternative is to read other profiles' `jobs.json` from disk
# directly (cron.jobs.load_jobs honours its module-level paths).
# ---------------------------------------------------------------------------


_CRON_PROFILE_LOCK = threading.RLock()


def _cron_profile_home(profile: Optional[str]) -> Tuple[str, Path]:
    """Resolve ``profile`` to ``(canonical_name, hermes_home_path)``.

    Mirrors ``hermes_cli/web_server.py:_cron_profile_home``. Raises
    ``ValueError`` for an invalid name and ``LookupError`` for an
    unknown profile so the caller can map to 400 / 404.
    """
    raw = (profile or "default").strip() or "default"
    try:
        from hermes_cli import profiles as profiles_mod  # type: ignore

        canon = profiles_mod.normalize_profile_name(raw)
        try:
            profiles_mod.validate_profile_name(canon)
        except AttributeError:
            # Older Hermes may not have validate_profile_name.
            pass
        if not profiles_mod.profile_exists(canon):
            raise LookupError(f"profile {canon!r} does not exist")
        return canon, profiles_mod.get_profile_dir(canon)
    except (ImportError, AttributeError):
        # No profile helpers available — single-profile mode. Only
        # accept the running process's profile (or "default" alias).
        own = _current_profile_name()
        if raw not in (own, "default"):
            raise LookupError(f"profile {raw!r} does not exist")
        return own, hermes_home()


def _list_known_profile_homes() -> List[Tuple[str, Path]]:
    """All profiles visible to this process, oldest-first by name.

    Falls back to a single-entry list (the running process's home) when
    ``hermes_cli.profiles.list_profiles`` isn't importable.
    """
    try:
        from hermes_cli import profiles as profiles_mod  # type: ignore

        items: List[Tuple[str, Path]] = []
        for p in profiles_mod.list_profiles():
            name = getattr(p, "name", None) or (p.get("name") if isinstance(p, dict) else None)
            if not isinstance(name, str) or not name:
                continue
            try:
                items.append((name, profiles_mod.get_profile_dir(name)))
            except Exception:
                continue
        if items:
            return items
    except Exception:
        pass
    return [(_current_profile_name(), hermes_home())]


def _annotate(job: Dict[str, Any], profile: str, home: Path) -> Dict[str, Any]:
    """Add upstream `_annotate_cron_job`'s 4 fields to a job dict."""
    out = dict(job)
    out["profile"] = profile
    out["profile_name"] = profile
    out["hermes_home"] = str(home)
    out["is_default_profile"] = profile == "default"
    return out


def _call_for_profile(profile: Optional[str], func_name: str, *args, **kwargs):
    """Run a ``cron.jobs`` helper against ``profile``'s HERMES_HOME.

    Same pattern upstream uses: temporarily swap ``cron.jobs.CRON_DIR``
    / ``JOBS_FILE`` / ``OUTPUT_DIR`` under a lock, run the helper,
    restore. Annotates list / dict results with the four profile-
    annotation fields so the response shape matches
    ``hermes_cli/web_server.py``'s.
    """
    profile_name, home = _cron_profile_home(profile)
    with _CRON_PROFILE_LOCK:
        from cron import jobs as cron_jobs  # type: ignore

        old_cron_dir = cron_jobs.CRON_DIR
        old_jobs_file = cron_jobs.JOBS_FILE
        old_output_dir = cron_jobs.OUTPUT_DIR
        cron_jobs.CRON_DIR = home / "cron"
        cron_jobs.JOBS_FILE = cron_jobs.CRON_DIR / "jobs.json"
        cron_jobs.OUTPUT_DIR = cron_jobs.CRON_DIR / "output"
        try:
            result = getattr(cron_jobs, func_name)(*args, **kwargs)
        finally:
            cron_jobs.CRON_DIR = old_cron_dir
            cron_jobs.JOBS_FILE = old_jobs_file
            cron_jobs.OUTPUT_DIR = old_output_dir

    if isinstance(result, list):
        return [_annotate(j, profile_name, home) for j in result if isinstance(j, dict)]
    if isinstance(result, dict):
        return _annotate(result, profile_name, home)
    return result


def _find_job_profile(job_id: str) -> Optional[str]:
    """Search known profiles for the one owning ``job_id``. Returns name or None."""
    for name, _home in _list_known_profile_homes():
        try:
            job = _call_for_profile(name, "get_job", job_id)
        except Exception:
            continue
        if job:
            return name
    return None


# ---------------------------------------------------------------------------
# Upstream-backed helpers — defensive imports so the bridge can still return
# a structured "cron unavailable" error when running outside a Hermes install.
# ---------------------------------------------------------------------------


def _cron_unavailable_error(exc: Exception) -> str:
    return (
        "Hermes cron module not importable (is the bridge running inside "
        f"Hermes Agent's venv?): {exc}"
    )


# Legacy deliver alias: "inbox" → "local". An earlier frontend used
# "inbox" to mean "file-only, no channel push"; we keep accepting it
# (and normalise it back to the canonical "local") so saved configs
# from that era still round-trip cleanly.
def _normalise_deliver(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() == "inbox":
        return "local"
    return value


def _current_profile_name() -> str:
    """Resolve the running process's profile name from ``$HERMES_HOME``.

    Mirrors what upstream ``hermes_cli.profiles.get_active_profile_name``
    does (``$HERMES_HOME`` → ``"default"`` / ``<name>`` / ``"custom"``).
    Falls back to ``"default"`` when the helper isn't importable so the
    annotation always carries *something* sane.
    """
    try:
        from hermes_cli.profiles import get_active_profile_name  # type: ignore

        return str(get_active_profile_name() or "default")
    except Exception:
        return "default"


def list_jobs_response(profile: str = "all") -> Dict[str, Any]:
    """List cron jobs, optionally scoped to a profile.

    ``profile="all"`` aggregates every known profile (matches upstream
    default). ``profile="<name>"`` lists only that profile's jobs.
    """
    requested = (profile or "all").strip() or "all"
    try:
        if requested.lower() == "all":
            jobs: List[Dict[str, Any]] = []
            for name, _home in _list_known_profile_homes():
                try:
                    jobs.extend(_call_for_profile(name, "list_jobs", True))
                except LookupError:
                    continue
                except Exception:
                    logger.exception("list_jobs failed for profile %s", name)
            return {"ok": True, "jobs": jobs}
        return {
            "ok": True,
            "jobs": _call_for_profile(requested, "list_jobs", True),
        }
    except LookupError as exc:
        return {"ok": False, "error": str(exc), "jobs": []}
    except Exception as exc:
        logger.exception("list_jobs failed")
        return {"ok": False, "error": f"list_jobs failed: {exc}", "jobs": []}


def get_job_response(job_id: str, profile: Optional[str] = None) -> Dict[str, Any]:
    selected = profile or _find_job_profile(job_id)
    if not selected:
        return {"ok": False, "error": "job not found"}
    try:
        job = _call_for_profile(selected, "get_job", job_id)
    except LookupError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("get_job %s failed", job_id)
        return {"ok": False, "error": f"get_job failed: {exc}"}
    if not job:
        return {"ok": False, "error": "job not found"}
    return {"ok": True, "job": job}


def create_job_response(
    payload: Dict[str, Any], profile: Optional[str] = None
) -> Dict[str, Any]:
    """Create a new cron job in ``profile`` (default: ``"default"``).

    Body validation kept here (rather than letting ``cron.jobs.create_job``
    raise) so error messages stay shaped consistently with the rest of
    the wrapper. Accepts the full ``cron.jobs.create_job`` kwarg set —
    upstream's pydantic restricts to 4 keys but the underlying function
    takes more; we pass through everything ``cron.jobs`` understands so
    agents calling via shell get the full surface.
    """
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
        job = _call_for_profile(profile or "default", "create_job", **kwargs)
    except LookupError as exc:
        return {"ok": False, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("create_job failed")
        return {"ok": False, "error": f"create_job failed: {exc}"}
    return {"ok": True, "job": job}


def update_job_response(
    job_id: str, updates: Dict[str, Any], profile: Optional[str] = None
) -> Dict[str, Any]:
    """Pass-through to ``cron.jobs.update_job(job_id, updates)`` in ``profile``.

    Strict mirror of upstream ``hermes_cli/web_server.py:update_cron_job``:
    forwards the entire ``body.updates`` dict (no field whitelist, no
    empty-dict rejection, no deliver-alias normalisation). Anything
    upstream's pydantic model would accept gets through.
    """
    selected = profile or _find_job_profile(job_id)
    if not selected:
        return {"ok": False, "error": "job not found"}
    try:
        job = _call_for_profile(selected, "update_job", job_id, updates)
    except LookupError as exc:
        return {"ok": False, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("update_job %s failed", job_id)
        return {"ok": False, "error": f"update_job failed: {exc}"}
    if not job:
        return {"ok": False, "error": "job not found"}
    return {"ok": True, "job": job}


def _lifecycle_op(
    job_id: str, op_name: str, profile: Optional[str] = None
) -> Dict[str, Any]:
    if op_name not in ("pause_job", "resume_job", "trigger_job"):
        return {"ok": False, "error": f"unknown op: {op_name}"}
    selected = profile or _find_job_profile(job_id)
    if not selected:
        return {"ok": False, "error": "job not found"}
    try:
        job = _call_for_profile(selected, op_name, job_id)
    except LookupError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("%s %s failed", op_name, job_id)
        return {"ok": False, "error": f"{op_name} failed: {exc}"}
    if not job:
        return {"ok": False, "error": "job not found"}
    return {"ok": True, "job": job}


def pause_job_response(job_id: str, profile: Optional[str] = None) -> Dict[str, Any]:
    return _lifecycle_op(job_id, "pause_job", profile)


def resume_job_response(job_id: str, profile: Optional[str] = None) -> Dict[str, Any]:
    return _lifecycle_op(job_id, "resume_job", profile)


def trigger_job_response(job_id: str, profile: Optional[str] = None) -> Dict[str, Any]:
    return _lifecycle_op(job_id, "trigger_job", profile)


def delete_job_response(job_id: str, profile: Optional[str] = None) -> Dict[str, Any]:
    selected = profile or _find_job_profile(job_id)
    if not selected:
        return {"ok": False, "error": "job not found"}
    try:
        removed = _call_for_profile(selected, "remove_job", job_id)
    except LookupError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("remove_job %s failed", job_id)
        return {"ok": False, "error": f"remove_job failed: {exc}"}
    if not removed:
        return {"ok": False, "error": "job not found"}
    return {"ok": True}
