"""Runtime registry for ``/integrations/<name>/*`` routes.

Holds a thread-safe map ``<name> → IntegrationRouter``. A single catch-all
aiohttp route in :mod:`runtime.dispatch` resolves each incoming request
against this map.

Why a custom registry instead of aiohttp sub-apps
-------------------------------------------------
aiohttp freezes a ``web.Application`` when ``AppRunner.setup()`` runs.
After that, ``add_subapp`` raises. That conflicts with two requirements
of this plugin:

1. Hermes loads plugins sequentially; an integration may queue itself
   for registration in the small window between *us* booting and the
   later plugin running.
2. ``integration_install`` (an agent tool) wants to mount new
   integrations while the server is live.

This module sidesteps the freeze problem entirely by never touching the
aiohttp Application after build_http_app. Routes live in this dict;
dispatch reads it at request time.

Public surface
--------------
- :func:`register_integration` — add or (with ``replace=True``) replace
  the routes for one ``<name>``. Safe from any thread, at any time.
- :func:`unregister_integration` — drop a ``<name>``. Used by
  ``integration_remove``.
- :class:`IntegrationRouter` — what ``setup(router)`` callables see.
  Surface matches the subset of ``aiohttp.web.UrlDispatcher`` that
  integrations actually use (``add_get / add_post / add_delete /
  add_patch / add_put / add_route``).

Naming
------
``name`` is the URL prefix segment. Must match ``^[a-z][a-z0-9-]*$``.
Examples: ``lark``, ``slack-bot``, ``zendesk``.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from aiohttp import web

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_NAME_MAX_LEN = 32

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
RouteSetupFn = Callable[[object], None]  # called with an IntegrationRouter

# aiohttp path template parts: ``/foo/{id}`` or ``/foo/{id:[0-9]+}``. The
# optional ``:regex`` lets integrations match more than the default
# ``[^/]+`` per segment.
_PATH_PARAM_RE = re.compile(r"\{(\w+)(?::([^}]+))?\}")


def _compile_path(path: str) -> "re.Pattern[str]":
    """Compile an aiohttp-style path template to a fullmatch regex.

    Output pattern is intended for ``fullmatch`` against the request tail
    (the portion after ``/integrations/<name>``). Param values land in
    named groups so the dispatcher can merge them into ``request.match_info``.
    """
    parts: List[str] = []
    pos = 0
    for m in _PATH_PARAM_RE.finditer(path):
        parts.append(re.escape(path[pos : m.start()]))
        name = m.group(1)
        body = m.group(2) or r"[^/]+"
        parts.append(f"(?P<{name}>{body})")
        pos = m.end()
    parts.append(re.escape(path[pos:]))
    return re.compile("".join(parts))


class IntegrationRouter:
    """Captures (method, path, handler) tuples for one integration.

    What ``setup(router)`` callables interact with. The surface mirrors
    ``aiohttp.web.UrlDispatcher`` for the common verbs so existing
    integration code (written against the old sub-app model) drops in
    unchanged.

    Reserved match-info keys: ``name`` and ``tail`` — populated by the
    catch-all route on the main app. Don't use those names for your own
    path params or the dispatcher's update will overwrite the path
    segments the catch-all extracted.
    """

    def __init__(self) -> None:
        self._routes: List[Tuple[str, "re.Pattern[str]", Handler]] = []

    def add_route(self, method: str, path: str, handler: Handler) -> None:
        if not callable(handler):
            raise TypeError("handler must be an awaitable callable")
        self._routes.append((method.upper(), _compile_path(path), handler))

    def add_get(self, path: str, handler: Handler) -> None:
        self.add_route("GET", path, handler)

    def add_post(self, path: str, handler: Handler) -> None:
        self.add_route("POST", path, handler)

    def add_delete(self, path: str, handler: Handler) -> None:
        self.add_route("DELETE", path, handler)

    def add_patch(self, path: str, handler: Handler) -> None:
        self.add_route("PATCH", path, handler)

    def add_put(self, path: str, handler: Handler) -> None:
        self.add_route("PUT", path, handler)

    def routes(self) -> List[Tuple[str, "re.Pattern[str]", Handler]]:
        """Return a shallow copy so callers can iterate without the lock."""
        return list(self._routes)


# Thread-safe registry. Both writers (loader at boot, tool handlers
# post-boot) and the dispatch reader hit this; mutations happen under
# the lock, reads grab a snapshot of the per-integration router which
# is itself effectively immutable once set (we replace, never mutate
# in place, on register/unregister).
_lock = threading.Lock()
_integrations: Dict[str, IntegrationRouter] = {}


def _validate_name(name: object) -> None:
    if (
        not isinstance(name, str)
        or not _NAME_RE.match(name)
        or len(name) > _NAME_MAX_LEN
    ):
        raise ValueError(
            f"invalid integration name {name!r}; "
            f"must match {_NAME_RE.pattern} (max {_NAME_MAX_LEN} chars)"
        )


def register_integration(
    name: str, setup: RouteSetupFn, *, replace: bool = False
) -> None:
    """Register integration *name*'s routes.

    Default is first-write-wins: a second call with the same name is
    a no-op (logged). Pass ``replace=True`` from the install / reload
    paths to atomically swap the router for an existing name; in-flight
    requests against the old router finish on the old handlers because
    the dispatcher copies the route list per request.

    Safe to call before or after the HTTP server is up, from any thread.
    """
    _validate_name(name)
    if not callable(setup):
        raise TypeError("register_integration: setup must be callable(router)")

    router = IntegrationRouter()
    setup(router)

    with _lock:
        existed = name in _integrations
        if existed and not replace:
            logger.info(
                "[backplane] register_integration: %r already registered, "
                "ignoring duplicate (pass replace=True to override)",
                name,
            )
            return
        _integrations[name] = router

    logger.info(
        "[backplane] %s integration: /integrations/%s/",
        "replaced" if existed else "registered",
        name,
    )


def unregister_integration(name: str) -> bool:
    """Drop *name* from the registry. Returns True if something was removed.

    Subsequent requests to ``/integrations/<name>/*`` get a 404 from the
    dispatcher. In-flight requests keep their already-resolved handler
    reference and complete normally.
    """
    with _lock:
        removed = _integrations.pop(name, None)
    if removed is not None:
        logger.info("[backplane] unregistered integration: /integrations/%s/", name)
        return True
    return False


def lookup(name: str) -> Optional[IntegrationRouter]:
    """Return the router for *name*, or None. Used by the dispatcher."""
    with _lock:
        return _integrations.get(name)


def list_names() -> List[str]:
    """Snapshot of currently-registered integration names."""
    with _lock:
        return sorted(_integrations.keys())
