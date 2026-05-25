"""
Backplane HTTP server entry point.

Run as a subprocess by the plugin's ``register()`` hook:

    python -m hermes_plugin_http_backplane.server --port 9394

The server hosts three lanes (see ``features/__init__.py``):
- ``/extension/*``  — browser-extension-private routes (file attach)
- ``/hermes/*``     — proxies to Hermes core APIs the gateway doesn't expose
- ``/integrations/{name}/*`` — third-party plugin routes (registered via
                                the ``register_integration`` public API)
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from aiohttp import web

from .http_app import build_http_app

logger = logging.getLogger(__name__)


async def _main(port: int) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

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

    try:
        await asyncio.Future()  # run forever
    finally:
        await runner.cleanup()


def main() -> None:
    try:
        from .adapters.dotenv_local import apply_plugin_dotenv

        apply_plugin_dotenv()
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="hermes-plugin-http-backplane HTTP server")
    parser.add_argument("--port", type=int, default=9394, help="HTTP listen port")
    args = parser.parse_args()
    asyncio.run(_main(args.port))


if __name__ == "__main__":
    main()
