"""
Internal queue API behind ``/integrations/{name}/*``.

All paths that add a route under ``/integrations/`` funnel through here:

- The loader (``features.integrations.loader``) calls it once per preset
  and once per user integration during HTTP-app boot.
- The ``integration_install`` tool calls it on the fly when the agent
  writes a new user integration.

The legacy "other plugins import this directly" path still works for
in-tree experimentation, but the supported way to add an integration is
to drop a directory under ``~/.hermes/integrations/<name>/`` (or under
``presets/`` for built-ins) and let the loader find it.

Lifecycle
---------
Calls before the HTTP server is up are queued and mounted on startup;
calls after are mounted immediately. ``_app_ref`` is the switch.

Naming
------
``name`` is the URL prefix segment. Must match ``^[a-z][a-z0-9-]*$``.
Examples: ``lark``, ``slack-bot``, ``zendesk``.
"""

from __future__ import annotations

import re
import threading
from typing import Callable, List, NamedTuple, Optional

# Type alias: a setup function receives a fresh aiohttp router scoped to
# ``/integrations/{name}/`` and registers routes on it (no prefix needed).
# Typed as ``object`` to avoid importing aiohttp at module load time —
# integration plugins can import the API without aiohttp installed if
# they only conditionally call register_integration.
RouteSetupFn = Callable[[object], None]

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_NAME_MAX_LEN = 32


class Integration(NamedTuple):
    """A registered integration awaiting (or already given) a route mount."""

    name: str
    setup: RouteSetupFn


# Thread-safe registry. Used both to queue pre-startup registrations
# and to look up post-startup ones (the queue is replayed once the HTTP
# server boots and then drained — see ``integrations_mount.mount_all``).
_lock = threading.Lock()
_pending: List[Integration] = []
_mounted: List[str] = []
_app_ref: Optional[object] = None  # set once HTTP server is up


def register_integration(name: str, setup: RouteSetupFn) -> None:
    """Mount integration *name*'s routes under ``/integrations/{name}/*``.

    Safe to call before or after the backplane HTTP server starts. Idempotent
    on (name, setup) pairs — a duplicate registration is a no-op (logged).
    """
    if not isinstance(name, str) or not _NAME_RE.match(name) or len(name) > _NAME_MAX_LEN:
        raise ValueError(
            f"register_integration: invalid name {name!r}; "
            f"must match {_NAME_RE.pattern} (max {_NAME_MAX_LEN} chars)"
        )
    if not callable(setup):
        raise TypeError("register_integration: setup must be callable(router)")

    with _lock:
        if name in _mounted:
            # Already live — re-registration silently ignored (logged
            # by integrations_mount when it sees the duplicate).
            return
        # De-dupe by name+setup identity in the queue.
        for entry in _pending:
            if entry.name == name and entry.setup is setup:
                return
        _pending.append(Integration(name=name, setup=setup))
        app = _app_ref

    # If the server is already running, mount this one immediately —
    # done outside the lock to avoid holding it during aiohttp calls.
    if app is not None:
        from .integrations_mount import mount_one  # local import: avoid cycle

        mount_one(app, name, setup)


def _drain_pending() -> List[Integration]:
    """Internal: pop all queued integrations. Called by mount_all on startup."""
    with _lock:
        out, _pending[:] = _pending[:], []
        return out


def _mark_mounted(name: str) -> None:
    """Internal: track a name as live so duplicate registrations no-op."""
    with _lock:
        if name not in _mounted:
            _mounted.append(name)


def _set_app(app: object) -> None:
    """Internal: store the aiohttp app once the HTTP server is up.

    Subsequent ``register_integration`` calls will mount immediately
    rather than queue.
    """
    global _app_ref
    with _lock:
        _app_ref = app


def _clear_app() -> None:
    """Internal: drop the app reference on HTTP server shutdown."""
    global _app_ref
    with _lock:
        _app_ref = None
        _mounted.clear()
