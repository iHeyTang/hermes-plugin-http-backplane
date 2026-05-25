"""Mount queued integrations onto the aiohttp app on HTTP server startup."""

from __future__ import annotations

import logging
from typing import Callable

from aiohttp import web

from . import api as _api

logger = logging.getLogger(__name__)


def mount_one(
    app: web.Application,
    name: str,
    setup: Callable[[web.UrlDispatcher], None],
) -> None:
    """Mount a single integration's routes under ``/integrations/{name}/*``.

    Uses aiohttp's sub-application support so the integration's *setup*
    only sees routes relative to its mount prefix. Errors in *setup* are
    logged and the integration is skipped — a broken integration must
    not break the backplane HTTP server.
    """
    try:
        sub = web.Application()
        setup(sub.router)
        app.add_subapp(f"/integrations/{name}/", sub)
        _api._mark_mounted(name)
        logger.info("[backplane] mounted integration: /integrations/%s/", name)
    except Exception as exc:
        logger.error(
            "[backplane] integration %s setup failed (skipped): %s",
            name, exc,
        )


def mount_all(app: web.Application) -> None:
    """Drain the pending-queue and mount every integration registered so far.

    Called once, by the HTTP server, after the main ``web.Application``
    is built but before ``run_app``. After this point, any subsequent
    ``register_integration`` calls will mount immediately (see api.py).
    """
    pending = _api._drain_pending()
    for entry in pending:
        mount_one(app, entry.name, entry.setup)
    _api._set_app(app)


def teardown(_app: web.Application) -> None:
    """Hook for HTTP server shutdown: drop the app ref so future calls re-queue."""
    _api._clear_app()
