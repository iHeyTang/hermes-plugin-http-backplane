"""HTTP surface for the ``hermes-integration`` CLI.

Three endpoints, all under ``/hermes/integrations/`` (distinct from the
runtime dispatch prefix ``/integrations/<name>/`` so admin actions can't
collide with an integration named ``reload`` or ``unregister``):

- ``GET    /hermes/integrations`` — snapshot of what's loaded
- ``POST   /hermes/integrations/reload?name=X`` — re-import + swap
- ``DELETE /hermes/integrations/{name}`` — unregister + delete files

Manager exceptions are translated to HTTP status codes; the body
mirrors the manager's plain dicts.
"""

from __future__ import annotations

from aiohttp import web

from ....common import json_error
from ...integrations import manager


def _error_status(exc: manager.IntegrationError) -> int:
    if isinstance(exc, manager.NameInvalid):
        return 400
    if isinstance(exc, manager.NameReserved):
        return 409
    if isinstance(exc, manager.NameTaken):
        return 409
    if isinstance(exc, manager.NotFound):
        return 404
    return 400


async def handle_list(_request: web.Request) -> web.Response:
    return web.json_response(manager.list_integrations())


async def handle_reload(request: web.Request) -> web.Response:
    name = request.query.get("name", "")
    try:
        return web.json_response(manager.reload(name))
    except manager.IntegrationError as exc:
        return json_error(_error_status(exc), str(exc))


async def handle_remove(request: web.Request) -> web.Response:
    name = request.match_info.get("name", "")
    try:
        return web.json_response(manager.remove(name))
    except manager.IntegrationError as exc:
        return json_error(_error_status(exc), str(exc))


def register(app: web.Application) -> None:
    app.add_routes(
        [
            web.get("/hermes/integrations", handle_list),
            web.post("/hermes/integrations/reload", handle_reload),
            web.delete("/hermes/integrations/{name}", handle_remove),
        ]
    )
