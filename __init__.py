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


# Mirrors hermes_cli.main._AGENT_COMMANDS / _AGENT_SUBCOMMANDS as of
# hermes-agent main.py:10678. Sync if Hermes adds a new long-running
# mode. A miss here means "client tries to reach the backplane, gets
# connection refused" — fail-loud, easy to spot. A false positive just
# spins up an unused server thread that will silently lose the port-bind
# race with the actually-serving process, which is harmless.
_HERMES_AGENT_COMMANDS = frozenset({None, "chat", "acp", "rl"})
_HERMES_AGENT_SUBCOMMANDS = {
    "cron": frozenset({"run", "tick"}),
    "gateway": frozenset({"run"}),
    "mcp": frozenset({"serve"}),
}


def _first_positional(argv: list[str]) -> Optional[str]:
    """Best-effort: skip leading ``-flag`` / ``--flag`` tokens, return the
    first positional, or None if there isn't one.

    Doesn't try to understand flag/value pairings (``--foo bar``) — that
    would couple us to Hermes's full flag taxonomy. Worst case for a
    flag-with-value: we treat the value as the subcommand, see it's not
    an agent command, and skip server startup. The actually-running
    Hermes process in the other terminal still has the server up, so
    nothing breaks; the CLI still hits it over HTTP.
    """
    for tok in argv:
        if tok.startswith("-"):
            continue
        return tok
    return None


def _is_agent_invocation() -> bool:
    """True when sys.argv suggests this process is going to run an agent.

    Used to gate ``start_server()`` so one-shot CLI invocations
    (``hermes integration list``, ``hermes plugins …``, etc.) don't
    bother spinning up an HTTP server thread that will just contend for
    port 9394 with whichever process is actually serving.

    ``HERMES_BACKPLANE_FORCE_START=1`` overrides the heuristic for
    testing / debugging.
    """
    if os.environ.get("HERMES_BACKPLANE_FORCE_START") == "1":
        return True
    argv = _sys.argv[1:]
    # Defense in depth: Hermes itself short-circuits --help / --version
    # before discover_plugins runs, so register() shouldn't be called in
    # those paths. If it IS, don't waste cycles on a server thread.
    if any(tok in {"-h", "--help", "-V", "--version"} for tok in argv):
        return False
    first = _first_positional(argv)
    if first in _HERMES_AGENT_COMMANDS:
        # ``None`` matches bare ``hermes`` (defaults to chat) per
        # hermes_cli's own taxonomy.
        return True
    valid_sub = _HERMES_AGENT_SUBCOMMANDS.get(first or "")
    if valid_sub is None:
        return False
    # Find the sub-subcommand after ``first``: e.g. ``hermes cron run``.
    try:
        idx = argv.index(first) + 1  # type: ignore[arg-type]
    except ValueError:
        return False
    sub = _first_positional(argv[idx:])
    return sub in valid_sub


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
    """Start the HTTP server in a daemon thread (idempotent + mode-gated).

    Returns immediately. We don't wait for server-ready because there's
    no longer a reason to: integration registrations land in a runtime
    dict (see runtime/api.py), and the dispatcher reads it per-request,
    so plugins loaded after us can call ``register_integration`` at any
    point and the routes go live without re-touching the aiohttp app.
    Keeping this non-blocking also means Hermes's sequential plugin load
    isn't slowed down by us binding the TCP port.

    No-op when :func:`_is_agent_invocation` says the current process is
    a one-shot CLI (``hermes integration list``, etc.) — it would just
    contend for the port with the actually-serving process and lose.
    Bypass with ``HERMES_BACKPLANE_FORCE_START=1``.
    """
    global _server_thread, _server_stop_event
    if _server_thread is not None and _server_thread.is_alive():
        return

    if not _is_agent_invocation():
        logger.debug(
            "hermes-plugin-http-backplane: skipping server start "
            "(CLI-only invocation: argv=%r)",
            _sys.argv[1:],
        )
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
    """Hermes plugin entry point: start the HTTP server + wire the
    ``hermes integration`` CLI subcommand.

    No agent tools are registered. Integration lifecycle (install /
    remove / reload / list) is operator-facing and exposed as the
    ``hermes integration ...`` subcommand of the umbrella ``hermes``
    CLI, which talks over loopback HTTP to the admin endpoints under
    ``/hermes/integrations/*``. Built-in presets (e.g. ``lark``) are
    discovered and registered during HTTP-app boot — see
    ``runtime/features/integrations``.

    ``register_cli_command`` and ``register_tool`` both come through
    ``ctx`` but target different surfaces: tool registration exposes a
    capability to the LLM (wrong layer for lifecycle), while CLI
    registration just plugs into Hermes's argparse tree (correct layer).
    """
    logger.info("Registering hermes-plugin-http-backplane")
    start_server()

    from . import cli as _cli

    ctx.register_cli_command(
        name="integration",
        help="Manage HTTP backplane integrations (list / install / remove / reload)",
        setup_fn=_cli.register_subparser,
        handler_fn=_cli.run,
        description=(
            "Operator-facing lifecycle commands for user integrations "
            "served at /integrations/<name>/* on the local HTTP backplane "
            "(default 127.0.0.1:9394). When the backplane is reachable, "
            "file-changing subcommands also trigger a live re-register "
            "so changes take effect without restarting Hermes."
        ),
    )

    logger.info(
        "hermes-plugin-http-backplane loaded (HTTP server + `hermes integration` CLI)"
    )
