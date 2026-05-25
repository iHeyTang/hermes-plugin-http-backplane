"""HTTP routes for ``/hermes/model/*`` — mirrors upstream ``/api/model/*``.

Path layout matches ``hermes_cli/web_server.py``:

- ``GET  /hermes/model/info``        — resolved metadata for the main model
- ``GET  /hermes/model/auxiliary``   — auxiliary slot assignments
- ``GET  /hermes/model/options``     — provider catalog + curated model lists
- ``POST /hermes/model/set``         — assign main or auxiliary slot

The single ``POST /hermes/model/set`` consolidates what used to be
``POST /hermes/main-model`` + ``POST /hermes/auxiliary-models``. The
``scope`` field dispatches: ``"main"`` writes ``model.*``,
``"auxiliary"`` (with ``task``) writes ``auxiliary.<task>.*``. The
sentinel ``task="__reset__"`` resets every auxiliary slot to
``provider="auto"``.

No mine-only additives on the ``/hermes/model/*`` surface — strict
parity with upstream. The mine-only ``/hermes/main-provider-settings``
GET+POST handles cases that need ``base_url`` (read on GET, written
on POST), so callers that need to clear a stale ``model.base_url``
route through there instead of ``/hermes/model/set``.

A mine-only endpoint that doesn't map to anything in upstream stays
under its current path: ``GET /hermes/provider-models?provider=...``
returns the per-provider model list for a single named provider.
"""

from __future__ import annotations

from urllib.parse import unquote

from aiohttp import web

from ....common import json_error, read_json_object, strip_ok
from .main_provider_settings_service import (
    read_main_provider_settings_response,
    save_main_provider_settings_response,
)
from .model_catalog_service import build_provider_models_http_response
from .model_config_service import (
    read_auxiliary_models_response,
    read_main_model_response,
    write_auxiliary_models_response,
    write_main_model_response,
)


# ---------------------------------------------------------------------------
# GET /hermes/model/info — main model resolution + capabilities
# ---------------------------------------------------------------------------


async def handle_model_info(_request: web.Request) -> web.Response:
    """Strict 1:1 with upstream ``GET /api/model/info``::

        {model, provider, auto_context_length, config_context_length,
         effective_context_length, capabilities}

    The adapter ``read_main_model`` also returns ``base_url`` for the
    mine-only ``/hermes/main-provider-settings`` endpoint to consume;
    we strip it here so the wire stays aligned with upstream.
    """
    try:
        payload = read_main_model_response()
    except RuntimeError as exc:
        return json_error(501, str(exc))
    data = strip_ok(payload)
    data.pop("base_url", None)
    return web.json_response(data)


# ---------------------------------------------------------------------------
# GET /hermes/model/auxiliary — auxiliary slot assignments
# ---------------------------------------------------------------------------


async def handle_model_auxiliary(_request: web.Request) -> web.Response:
    """Upstream shape, no additives::

        {"tasks": [{task, provider, model, base_url}, ...],
         "main":  {provider, model}}
    """
    try:
        payload = read_auxiliary_models_response()
    except RuntimeError as exc:
        return json_error(501, str(exc))
    return web.json_response(strip_ok(payload))


# ---------------------------------------------------------------------------
# POST /hermes/model/set — assign main or auxiliary slot
# ---------------------------------------------------------------------------


_AUX_RESET_SENTINEL = "__reset__"


async def handle_model_set(request: web.Request) -> web.Response:
    """Strict 1:1 with upstream POST /api/model/set.

    Body shape::

        {"scope":    "main" | "auxiliary",
         "provider": "<provider>",
         "model":    "<model id>",
         "task":     "<aux slot>" | "__reset__" | ""}

    When ``scope="auxiliary"`` and ``task=""``, upstream applies the
    (provider, model) pair to every aux slot; we do the same. When
    ``task="__reset__"``, every aux slot is reset to ``provider="auto"``
    and ``model=""``. ``main`` requires both ``provider`` and ``model``.
    """
    try:
        body = await read_json_object(request)
    except web.HTTPBadRequest as exc:
        return exc

    scope = str(body.get("scope") or "").strip().lower()
    provider = str(body.get("provider") or "").strip()
    model = str(body.get("model") or "").strip()
    task = str(body.get("task") or "").strip().lower()

    if scope not in {"main", "auxiliary"}:
        return json_error(400, "scope must be 'main' or 'auxiliary'")

    if scope == "main":
        if not provider or not model:
            return json_error(400, "provider and model required for main")
        try:
            write_main_model_response(
                {"provider": provider, "model": model}
            )
        except ValueError as exc:
            return json_error(400, str(exc))
        except RuntimeError as exc:
            return json_error(501, str(exc))
        return web.json_response(
            {"ok": True, "scope": "main", "provider": provider, "model": model}
        )

    # scope == "auxiliary"
    if task == _AUX_RESET_SENTINEL:
        from ....adapters.hermes_agent_model import AUXILIARY_SLOTS

        try:
            for slot in AUXILIARY_SLOTS:
                write_auxiliary_models_response(
                    {"task": slot, "provider": "auto", "model": ""}
                )
        except ValueError as exc:
            return json_error(400, str(exc))
        except RuntimeError as exc:
            return json_error(501, str(exc))
        return web.json_response({"ok": True, "scope": "auxiliary", "reset": True})

    if not provider:
        return json_error(400, "provider required for auxiliary")

    from ....adapters.hermes_agent_model import AUXILIARY_SLOTS

    targets = [task] if task else list(AUXILIARY_SLOTS)
    for slot in targets:
        if slot not in AUXILIARY_SLOTS:
            return json_error(400, f"unknown auxiliary task: {slot}")
        try:
            write_auxiliary_models_response(
                {"task": slot, "provider": provider, "model": model}
            )
        except ValueError as exc:
            return json_error(400, str(exc))
        except RuntimeError as exc:
            return json_error(501, str(exc))

    return web.json_response(
        {
            "ok": True,
            "scope": "auxiliary",
            "tasks": targets,
            "provider": provider,
            "model": model,
        }
    )


# ---------------------------------------------------------------------------
# GET /hermes/model/options — provider catalog + curated model lists
# ---------------------------------------------------------------------------


async def handle_model_options(_request: web.Request) -> web.Response:
    """Strict 1:1 with upstream ``GET /api/model/options``.

    Delegates to ``hermes_cli.inventory.build_models_payload`` so the
    wire shape (``{providers (list), model, provider}``) is identical.
    When the helper isn't importable (running outside a Hermes venv —
    development / test only), returns 501 rather than serving a
    differently-shaped local fallback.
    """
    try:
        from hermes_cli.inventory import (  # type: ignore
            build_models_payload,
            load_picker_context,
        )
    except Exception as exc:
        return json_error(
            501,
            f"hermes_cli.inventory unavailable (run inside Hermes venv): {exc}",
        )

    try:
        return web.json_response(
            build_models_payload(load_picker_context(), max_models=50)
        )
    except Exception as exc:
        return json_error(500, f"failed to list model options: {exc}")


# ---------------------------------------------------------------------------
# GET /hermes/provider-models — mine-only (no upstream equivalent)
# ---------------------------------------------------------------------------


async def handle_provider_models(request: web.Request) -> web.Response:
    refresh_pm = request.query.get("refresh", "0")
    force_pm = str(refresh_pm).lower() in ("1", "true", "yes")
    provider_pm = unquote(str(request.query.get("provider", ""))).strip()
    if not provider_pm:
        return json_error(400, "missing provider query parameter")
    body_pm = build_provider_models_http_response(
        provider=provider_pm, force_refresh=force_pm
    )
    return web.json_response(body_pm)


# ---------------------------------------------------------------------------
# GET/POST /hermes/main-provider-settings — mine-only (no upstream equivalent)
# ---------------------------------------------------------------------------


async def handle_main_provider_settings_get(request: web.Request) -> web.Response:
    cred_for = unquote(str(request.query.get("provider", ""))).strip() or None
    return web.json_response(
        read_main_provider_settings_response(credentials_for=cred_for)
    )


async def handle_main_provider_settings_post(request: web.Request) -> web.Response:
    payload = await read_json_object(request)
    try:
        return web.json_response(save_main_provider_settings_response(payload))
    except ValueError as exc:
        return json_error(400, str(exc))
    except RuntimeError as exc:
        return json_error(501, str(exc))
    except OSError as exc:
        return json_error(500, str(exc))


def register_model_routes(app: web.Application) -> None:
    app.add_routes(
        [
            web.get("/hermes/model/info", handle_model_info),
            web.get("/hermes/model/auxiliary", handle_model_auxiliary),
            web.get("/hermes/model/options", handle_model_options),
            web.post("/hermes/model/set", handle_model_set),
            # Mine-only beyond this line.
            web.get("/hermes/provider-models", handle_provider_models),
            web.get(
                "/hermes/main-provider-settings",
                handle_main_provider_settings_get,
            ),
            web.post(
                "/hermes/main-provider-settings",
                handle_main_provider_settings_post,
            ),
        ]
    )
