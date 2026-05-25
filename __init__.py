"""
hermes-plugin-http-backplane — HTTP server plugin for Hermes Agent.

Hosts three lanes of HTTP routes:
- ``/extension/*``     — extension-private (file attach)
- ``/hermes/*``        — proxies to Hermes core (cron, model catalog, …)
- ``/integrations/*``  — agent-managed endpoints

The backplane owns ``/integrations/*`` end-to-end. It ships with built-in
presets under ``runtime/features/integrations/presets/`` (e.g. ``lark``)
and exposes four ``integration_*`` tools so the Hermes agent can write
new integrations to ``~/.hermes/integrations/<name>/`` and hot-mount them
via conversation. The legacy ``register_integration`` Python API still
exists in ``runtime/api.py`` as the internal queue all paths funnel
through; it is no longer the recommended way for outside code to add
endpoints.

Architecture: the HTTP server runs in a **daemon thread** inside the
Hermes process. Both presets and user integrations queue their routes
into a Python list — that list MUST be in the same process where the
HTTP server reads it.

The server thread owns its own asyncio event loop; Hermes's main loop
is untouched. A panic in a route handler is caught by aiohttp and
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
        # Brief delay before building the app: Hermes loads plugins
        # sequentially, and integrations registered after ours need time
        # to call register_integration() (queueing into api._pending)
        # BEFORE we call build_http_app(), which drains the queue and
        # freezes the aiohttp app via AppRunner.setup(). Adding subapps
        # to a frozen app fails. 500ms covers in-order plugin loading
        # comfortably on a healthy machine; tighten or loosen via
        # HERMES_BACKPLANE_STARTUP_DELAY_MS env var if needed.
        delay_ms = int(os.environ.get("HERMES_BACKPLANE_STARTUP_DELAY_MS", "500"))
        await asyncio.sleep(delay_ms / 1000)

        app = build_http_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        logger.info(
            "hermes-plugin-http-backplane HTTP on http://127.0.0.1:%d — "
            "/extension/*, /hermes/*, /integrations/{name}/*",
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

    DOES NOT block on server-ready: we MUST return before Hermes loads
    the next plugin so any plugin that wants to queue routes via
    ``register_integration()`` can do so while ``_app_ref`` is still
    None. The server thread's own startup delay (default 500ms) gives
    those queued integrations — plus the loader's own preset and user
    discovery pass — time to land before ``build_http_app`` drains the
    queue and freezes the app via ``AppRunner.setup()``. Blocking here
    would invert that ordering and every integration would race into a
    frozen app.
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
        "hermes-plugin-http-backplane server thread spawned (port %d); HTTP comes up "
        "after the startup-delay window expires (default 500ms)",
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
    """Hermes plugin entry point: start the HTTP server, register tools.

    Exposes four ``integration_*`` tools so the agent can list, install,
    remove, and reload user integrations under ``/integrations/<name>/*``
    via conversation. Built-in presets (e.g. ``lark``) are discovered and
    mounted during HTTP-app boot — see ``runtime/features/integrations``.
    """
    logger.info("Registering hermes-plugin-http-backplane")
    start_server()

    from .runtime.features.integrations.tools import TOOLS

    for name, schema, handler, emoji in TOOLS:
        ctx.register_tool(
            name=name,
            toolset="integrations",
            schema=schema,
            handler=handler,
            emoji=emoji,
        )

    logger.info(
        "hermes-plugin-http-backplane loaded (HTTP server + %d integration tools)",
        len(TOOLS),
    )
