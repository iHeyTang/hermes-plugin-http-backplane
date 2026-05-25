"""
hermes-plugin-http-backplane — HTTP server plugin for Hermes Agent.

Hosts two lanes of HTTP routes:
- ``/hermes/*``        — wrappers over Hermes core (cron, sessions, model
                          catalog, settings, memory, skills, attachments)
- ``/integrations/*``  — agent-managed endpoints

The backplane owns ``/integrations/*`` end-to-end. It ships with built-in
presets under ``runtime/features/integrations/presets/`` (e.g. ``lark``)
and exposes four ``integration_*`` tools so the Hermes agent can write
new integrations to ``~/.hermes/integrations/<name>/`` and hot-mount them
via conversation. ``register_integration`` in ``runtime/api.py`` is the
underlying API both paths funnel through.

Architecture
------------
The HTTP server runs in a **daemon thread** inside the Hermes process
with its own asyncio event loop — Hermes's main loop is untouched.

``/integrations/*`` routes do **not** use aiohttp sub-apps. Instead,
a single catch-all route ``/integrations/{name}/{tail:.*}`` dispatches
at request time against a runtime-mutable registry of
``IntegrationRouter`` objects (see ``runtime/api.py``,
``runtime/dispatch.py``). This dodges aiohttp's "frozen Application
after AppRunner.setup()" constraint, so an integration registered any
time — at boot, mid-flight via the install tool, or by a late-loading
sibling plugin — is reachable on the next matching request without
restarting the server.

A panic in a route handler is caught by aiohttp / the dispatcher and
returned as a 500, never propagating into the Hermes process.
"""

from __future__ import annotations

import atexit
import logging
import os
import sys as _sys
import threading
from typing import Optional

# Re-export the public API so integrations can do:
#     from hermes_plugin_http_backplane import register_integration
from .runtime.api import register_integration  # noqa: F401

# When Hermes loads us via its directory-based plugin loader, our actual
# module name becomes ``hermes_plugins.hermes_plugin_http_backplane`` — NOT the
# top-level ``hermes_plugin_http_backplane`` other plugins try to import. Alias
# ourselves under the canonical name so ``from hermes_plugin_http_backplane
# import register_integration`` works regardless of load mechanism (pip
# install vs Hermes plugin discovery). Harmless when the canonical name
# was the real one already.
_sys.modules.setdefault("hermes_plugin_http_backplane", _sys.modules[__name__])

logger = logging.getLogger(__name__)

_server_thread: Optional[threading.Thread] = None
_server_stop_event: Optional[threading.Event] = None


def _get_port() -> int:
    return int(os.environ.get("HERMES_BACKPLANE_PORT", "9394"))


def _run_server_thread(port: int, ready: threading.Event, stop: threading.Event) -> None:
    """Body of the daemon thread: bring up aiohttp on its own event loop."""
    import asyncio

    from aiohttp import web

    from .runtime.http_app import build_http_app

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _main() -> None:
        # No startup delay needed: integrations live in a runtime-mutable
        # registry (see runtime/api.py), and a single catch-all aiohttp
        # route reads it at request time. Late register_integration calls
        # — from sibling plugins loaded after us, or from the
        # integration_install tool — take effect on the next matching
        # request without touching the (potentially frozen) Application.
        app = build_http_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        logger.info(
            "hermes-plugin-http-backplane HTTP on http://127.0.0.1:%d — "
            "/hermes/*, /integrations/{name}/*",
            port,
        )
        ready.set()

        # Translate the threading.Event into an async sleep so we can wait
        # for shutdown without blocking the loop. ``stop`` is set by the
        # atexit hook below.
        while not stop.is_set():
            await asyncio.sleep(0.5)

        await runner.cleanup()

    try:
        loop.run_until_complete(_main())
    except Exception:
        logger.exception("hermes-plugin-http-backplane server thread crashed")
    finally:
        loop.close()


def start_server() -> None:
    """Start the HTTP server in a daemon thread (idempotent).

    Returns immediately. We don't wait for server-ready because there's
    no longer a reason to: integration registrations land in a runtime
    dict (see runtime/api.py), and the dispatcher reads it per-request,
    so plugins loaded after us can call ``register_integration`` at any
    point and the routes go live without re-touching the aiohttp app.
    Keeping this non-blocking also means Hermes's sequential plugin load
    isn't slowed down by us binding the TCP port.
    """
    global _server_thread, _server_stop_event
    if _server_thread is not None and _server_thread.is_alive():
        return

    port = _get_port()
    _server_stop_event = threading.Event()
    # ``ready`` exists for diagnostics only — we don't wait on it.
    ready = threading.Event()
    _server_thread = threading.Thread(
        target=_run_server_thread,
        args=(port, ready, _server_stop_event),
        name="hermes-plugin-http-backplane",
        daemon=True,
    )
    _server_thread.start()
    logger.info(
        "hermes-plugin-http-backplane server thread spawned (port %d)",
        port,
    )


def stop_server() -> None:
    """Signal the server thread to drain and exit. Best-effort."""
    global _server_thread, _server_stop_event
    if _server_stop_event is not None:
        _server_stop_event.set()
    if _server_thread is not None:
        _server_thread.join(timeout=5.0)
        if _server_thread.is_alive():
            logger.warning("hermes-plugin-http-backplane: server thread did not stop in 5s")
    _server_thread = None
    _server_stop_event = None
    logger.info("hermes-plugin-http-backplane stopped")


atexit.register(stop_server)


def register(ctx) -> None:
    """Hermes plugin entry point: start the HTTP server.

    No agent tools are registered. Integration lifecycle (install /
    remove / reload / list) is operator-facing and lives in the
    standalone ``hermes-integration`` CLI plus the loopback admin
    endpoints under ``/hermes/integrations/*`` that the CLI talks to.
    Built-in presets (e.g. ``lark``) are discovered and registered
    during HTTP-app boot — see ``runtime/features/integrations``.
    """
    del ctx  # unused; kept for plugin-protocol compatibility
    logger.info("Registering hermes-plugin-http-backplane")
    start_server()
    logger.info("hermes-plugin-http-backplane loaded (HTTP server only)")
