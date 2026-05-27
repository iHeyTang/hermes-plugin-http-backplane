"""Logic behind ``/hermes/status`` + the two lifecycle actions.

Defensive throughout — every upstream import is local to the call site
so a missing optional dependency (e.g. ``gateway.config`` not available
in a stripped install) degrades to a partial payload instead of 500.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ....adapters.hermes_core import hermes_home

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Action registry — name → log file. Whitelist. New actions land here.
# Mirrors ``hermes_cli/web_server.py:_ACTION_LOG_FILES``.
# ---------------------------------------------------------------------------


ACTION_LOG_FILES: Dict[str, str] = {
    "gateway-restart": "gateway-restart.log",
    "hermes-update": "hermes-update.log",
}


def _action_log_dir() -> Path:
    return hermes_home() / "logs"


# Process-global table of most-recently-spawned Popen handles per action.
# Lock guards reads + writes since backplane is multi-threaded (HTTP
# handlers run in the server-thread's asyncio loop, but spawn-side cron /
# manual tools can poke us from any thread).
_ACTION_PROCS: Dict[str, subprocess.Popen] = {}
_ACTION_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Spawn + tail helpers
# ---------------------------------------------------------------------------


def spawn_hermes_action(subcommand: List[str], name: str) -> subprocess.Popen:
    """Run ``hermes <subcommand>`` detached + record the Popen handle.

    Uses the running interpreter's ``hermes_cli.main`` module so the
    spawned action inherits the same venv/PYTHONPATH backplane uses.
    stdin is closed so any stray ``input()`` in the action subprocess
    fails fast (EOF) instead of hanging forever.

    Raises ``KeyError`` if ``name`` isn't in :data:`ACTION_LOG_FILES`
    (caller should 404 before calling).
    """
    log_file_name = ACTION_LOG_FILES[name]
    log_dir = _action_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / log_file_name
    log_file = open(log_path, "ab", buffering=0)
    log_file.write(
        f"\n=== {name} started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode()
    )

    cmd = [sys.executable, "-m", "hermes_cli.main", *subcommand]

    popen_kwargs: Dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
        "env": {**os.environ, "HERMES_NONINTERACTIVE": "1"},
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    with _ACTION_LOCK:
        _ACTION_PROCS[name] = proc
    logger.info("[backplane] spawned action %s (pid=%d)", name, proc.pid)
    return proc


def _tail_lines(path: Path, n: int) -> List[str]:
    """Last ``n`` lines of ``path``. Whole-file read; per-action logs are small."""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[-n:] if n > 0 else lines


def action_status(name: str, lines: int = 200) -> Optional[Dict[str, Any]]:
    """Status snapshot of action ``name``, or None if ``name`` is unknown.

    Shape mirrors upstream:
    ``{name, running, exit_code, pid, lines}``
    """
    log_file_name = ACTION_LOG_FILES.get(name)
    if log_file_name is None:
        return None

    log_path = _action_log_dir() / log_file_name
    capped = min(max(int(lines or 0), 1), 2000)
    tail = _tail_lines(log_path, capped)

    with _ACTION_LOCK:
        proc = _ACTION_PROCS.get(name)
    if proc is None:
        running = False
        exit_code: Optional[int] = None
        pid: Optional[int] = None
    else:
        exit_code = proc.poll()
        running = exit_code is None
        pid = proc.pid

    return {
        "name": name,
        "running": running,
        "exit_code": exit_code,
        "pid": pid,
        "lines": tail,
    }


# ---------------------------------------------------------------------------
# /hermes/status — runtime snapshot
# ---------------------------------------------------------------------------


def _safe_import(modpath: str, *names: str) -> Optional[Tuple[Any, ...]]:
    """Import ``modpath`` and return a tuple of the requested attributes.

    Returns ``None`` (not a partial tuple) when import or any attr lookup
    fails — lets the caller branch cleanly with ``if x is None``.
    """
    try:
        mod = __import__(modpath, fromlist=list(names))
        return tuple(getattr(mod, n) for n in names)
    except Exception:
        return None


def _active_session_count() -> int:
    """Count sessions whose ``ended_at`` is None AND last_active < 5 min ago.

    Same definition upstream uses on ``/api/status``. Bound the scan to
    50 recent sessions so a giant SessionDB doesn't slow the status poll.
    """
    helpers = _safe_import("hermes_state", "SessionDB")
    if helpers is None:
        return 0
    SessionDB, = helpers
    try:
        db = SessionDB()
    except Exception:
        return 0
    try:
        try:
            sessions = db.list_sessions_rich(limit=50)
        except Exception:
            return 0
        now = time.time()
        return sum(
            1 for s in sessions
            if s.get("ended_at") is None
            and (now - s.get("last_active", s.get("started_at", 0))) < 300
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


def _version_info() -> Tuple[str, str]:
    """Return (current_version, release_date) — empty strings if unavailable."""
    helpers = _safe_import("hermes_cli", "__version__", "__release_date__")
    if helpers is None:
        return "", ""
    v, d = helpers
    return str(v or ""), str(d or "")


def _config_version_info() -> Tuple[Optional[int], Optional[int]]:
    """Return (current_config_version, latest_config_version)."""
    helpers = _safe_import("hermes_cli.config", "check_config_version")
    if helpers is None:
        return None, None
    check, = helpers
    try:
        return check()
    except Exception:
        return None, None


def _config_and_env_paths() -> Tuple[Optional[str], Optional[str]]:
    """Return string paths to config.yaml and .env, or (None, None)."""
    helpers = _safe_import("hermes_cli.config", "get_config_path", "get_env_path")
    if helpers is None:
        return None, None
    get_cfg, get_env = helpers
    try:
        return str(get_cfg()), str(get_env())
    except Exception:
        return None, None


def _gateway_pid() -> Optional[int]:
    helpers = _safe_import("gateway.status", "get_running_pid")
    if helpers is None:
        return None
    get_pid, = helpers
    try:
        return get_pid()
    except Exception:
        return None


def _gateway_runtime_status() -> Optional[Dict[str, Any]]:
    helpers = _safe_import("gateway.status", "read_runtime_status")
    if helpers is None:
        return None
    read, = helpers
    try:
        return read()
    except Exception:
        return None


def _configured_gateway_platforms() -> Optional[set]:
    helpers = _safe_import("gateway.config", "load_gateway_config")
    if helpers is None:
        return None
    load, = helpers
    try:
        cfg = load()
        return {p.value for p in cfg.get_connected_platforms()}
    except Exception:
        return None


def _update_check(force: bool = False) -> Dict[str, Any]:
    """Cheap "is there a new Hermes version?" query.

    Delegates to upstream ``hermes_cli.banner.check_for_updates`` which
    is cached for 6h under ``$HERMES_HOME/.update_check`` and picks the
    right source automatically (git ls-remote for git installs, pypi for
    pip installs). Returns one of three statuses so the UI can render
    a tight badge next to the Update button:

    - ``up_to_date``: matches latest known
    - ``behind`` + ``commits_behind`` (int) when count is known, else
      ``commits_behind: None`` (just "update available")
    - ``unknown``: the upstream check failed or couldn't run

    When ``force`` is true, the 6h cache file is deleted before the
    upstream check runs so the user gets a fresh remote probe — used by
    the Status page's Refresh button.
    """
    if force:
        try:
            (hermes_home() / ".update_check").unlink(missing_ok=True)
        except OSError:
            pass
    helpers = _safe_import(
        "hermes_cli.banner", "check_for_updates", "UPDATE_AVAILABLE_NO_COUNT"
    )
    if helpers is None:
        return {"status": "unknown", "commits_behind": None}
    check, no_count = helpers
    try:
        result = check()
    except Exception:
        return {"status": "unknown", "commits_behind": None}
    if result is None:
        return {"status": "unknown", "commits_behind": None}
    if result == 0:
        return {"status": "up_to_date", "commits_behind": 0}
    if result == no_count:
        return {"status": "behind", "commits_behind": None}
    try:
        return {"status": "behind", "commits_behind": int(result)}
    except (TypeError, ValueError):
        return {"status": "behind", "commits_behind": None}


async def status_response(force_update_check: bool = False) -> Dict[str, Any]:
    """Build the ``GET /hermes/status`` payload (upstream-aligned).

    Async because we may want to do a remote gateway health-probe in
    the future; for now everything is synchronous-in-a-thread-pool so
    we don't block the event loop on DB / filesystem reads.

    ``force_update_check`` busts the 6h update-check cache; only the
    explicit user-triggered Refresh sets it, the auto-poll uses cache.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _build_status_sync, force_update_check
    )


def _build_status_sync(force_update_check: bool = False) -> Dict[str, Any]:
    version, release_date = _version_info()
    cfg_v, latest_cfg_v = _config_version_info()
    config_path, env_path = _config_and_env_paths()

    gateway_pid = _gateway_pid()
    gateway_running = gateway_pid is not None
    runtime = _gateway_runtime_status()
    configured = _configured_gateway_platforms()

    gateway_state: Optional[str] = None
    gateway_platforms: Dict[str, Any] = {}
    gateway_exit_reason: Optional[str] = None
    gateway_updated_at: Optional[Any] = None

    if runtime:
        gateway_state = runtime.get("gateway_state")
        platforms = runtime.get("platforms") or {}
        if configured is not None:
            platforms = {k: v for k, v in platforms.items() if k in configured}
        gateway_platforms = platforms
        gateway_exit_reason = runtime.get("exit_reason")
        gateway_updated_at = runtime.get("updated_at")
        if not gateway_running:
            gateway_state = (
                gateway_state
                if gateway_state in {"stopped", "startup_failed"}
                else "stopped"
            )
            gateway_platforms = {}

    return {
        "version": version,
        "release_date": release_date,
        "hermes_home": str(hermes_home()),
        "config_path": config_path,
        "env_path": env_path,
        "config_version": cfg_v,
        "latest_config_version": latest_cfg_v,
        "gateway_running": gateway_running,
        "gateway_pid": gateway_pid,
        "gateway_state": gateway_state,
        "gateway_platforms": gateway_platforms,
        "gateway_exit_reason": gateway_exit_reason,
        "gateway_updated_at": gateway_updated_at,
        "active_sessions": _active_session_count(),
        "update_check": _update_check(force=force_update_check),
    }
