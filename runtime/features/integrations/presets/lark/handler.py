"""Route setup for the ``lark`` preset.

Mounted under ``/integrations/lark/`` by the loader. The ``setup``
callable receives a fresh aiohttp router scoped to that prefix — paths
registered here are relative.
"""

from __future__ import annotations

from aiohttp import web

from .lark_cli import search_all


def _parse_int(value: str | None, default: int) -> int:
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


async def handle_search(request: web.Request) -> web.Response:
    query = request.query.get("q", "")
    limit = _parse_int(request.query.get("limit"), 8)
    payload = await search_all(query, limit)
    # Always 200 — extension clients treat empty / error result as "fall
    # back to free-text", not as a transport failure.
    return web.json_response(payload)


def setup(router: web.UrlDispatcher) -> None:
    """Register lark routes under the backplane's ``/integrations/lark/`` mount."""
    router.add_get("/search", handle_search)
