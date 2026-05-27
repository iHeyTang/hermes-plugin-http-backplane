"""HTTP routes for ``/hermes/model/*`` — mirrors upstream ``/api/model/*``.

Path layout matches ``hermes_cli/web_server.py``:

- ``GET  /hermes/model/info``        — resolved metadata for the main model
- ``GET  /hermes/model/auxiliary``   — auxiliary slot assignments
- ``GET  /hermes/model/options``     — provider catalog + curated model lists
- ``POST /hermes/model/set``         — assign main or auxiliary slot
                                       (additive ``base_url`` field on ``scope=main``)

Mine-only endpoints (no upstream equivalent):

- ``GET  /hermes/provider-models?provider=…``  — per-provider model list
- ``GET  /hermes/provider-credentials?provider=…`` — plugin ``.env``
                                       credentials for one provider slug
- ``POST /hermes/provider-credentials`` — write credentials for one provider

Provider credentials are completely separated from main-model state:
writing credentials never touches ``config.yaml: model.*``. Setting the
main model is the sole responsibility of ``POST /hermes/model/set``.
"""

from __future__ import annotations

from urllib.parse import unquote

from aiohttp import web

from ....common import json_error, read_json_object, strip_ok
from .model_catalog_service import build_provider_models_http_response
from .model_config_service import (
    read_auxiliary_models_response,
    read_main_model_response,
    write_auxiliary_models_response,
    write_main_model_response,
)
from .provider_credentials_service import (
    merge_credentials_for_provider,
    read_provider_credentials_response,
)


# ---------------------------------------------------------------------------
# GET /hermes/model/info — main model resolution + capabilities
# ---------------------------------------------------------------------------


async def handle_model_info(_request: web.Request) -> web.Response:
    """Resolved metadata for the main model.

    Returns ``{model, provider, base_url, auto_context_length,
    config_context_length, effective_context_length, capabilities}``.
    ``base_url`` is mine-only (upstream omits it); it's the resolved
    ``model.base_url`` from ``config.yaml`` so the UI can show "what
    URL is my main model talking to right now".
    """
    try:
        payload = read_main_model_response()
    except RuntimeError as exc:
        return json_error(501, str(exc))
    return web.json_response(strip_ok(payload))


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
    """Single write surface for both main and auxiliary slots.

    Body shape::

        {"scope":    "main" | "auxiliary",
         "provider": "<provider>",
         "model":    "<model id>",
         "base_url": "<url>" | null | (omitted),     # scope=main only
         "task":     "<aux slot>" | "__reset__" | ""}

    On ``scope="main"`` the optional ``base_url`` is additive over
    upstream — passing ``null`` clears ``model.base_url`` (used when
    switching from a custom endpoint back to a canonical provider);
    omitting it leaves the current value alone. On ``scope="auxiliary"``
    with ``task=""`` the (provider, model) pair is applied to every aux
    slot. ``task="__reset__"`` resets every aux slot to ``auto`` / ``""``.
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
        write_payload: dict = {"provider": provider, "model": model}
        if "base_url" in body:
            write_payload["base_url"] = body.get("base_url")
        try:
            merged = write_main_model_response(write_payload)
        except ValueError as exc:
            return json_error(400, str(exc))
        except RuntimeError as exc:
            return json_error(501, str(exc))
        return web.json_response(merged)

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
    """Provider catalog + curated model lists.

    Delegates to ``hermes_cli.inventory.build_models_payload`` with two
    extra flags so the extension's adapter has the data it needs:

    - ``include_unconfigured=True`` — append canonical providers the
      user hasn't authenticated yet (otherwise the picker can only
      show what's already wired up — you can't discover what else
      exists).
    - ``picker_hints=True`` — add ``authenticated``/``auth_type``/
      ``key_env``/``warning`` per row so the adapter can tell
      "user-config", "env-detected" and "unconfigured" apart.

    Adds one mine-only field on top: ``configured_provider_slugs`` —
    the alias-resolved canonical slugs the user explicitly listed in
    ``config.yaml: providers:``. Needed because Hermes's
    ``list_authenticated_providers`` silently drops user-config rows
    when their slug is an alias of a canonical provider already
    emitted by the built-in/hermes overlay sections (e.g. user writes
    ``providers.vercel`` but the picker only shows ``ai-gateway``
    with ``source="hermes"`` — the ``user-config`` provenance is
    lost). The adapter uses this set to mark "Configured" badges
    correctly regardless of what ``source`` the row got.

    All extra fields are additive (upstream consumers ignore unknown
    keys), so this stays compatible with upstream's wire shape.

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
        ctx = load_picker_context()
        payload = build_models_payload(
            ctx,
            max_models=50,
            include_unconfigured=True,
            picker_hints=True,
        )
        payload["configured_provider_slugs"] = _user_configured_canonical_slugs(ctx)
        # Strict "user configured this via the extension" set — reads
        # ``~/.hermes/.env`` directly so ambient creds (host env vars,
        # Claude Code OAuth, gh CLI, AWS SDK, …) don't pollute the UI's
        # "Configured" group with providers the user never touched here.
        try:
            from ....adapters.hermes_provider_env import (
                provider_slugs_configured_in_dotenv,
            )
            row_slugs = [
                str(r.get("slug") or "").strip()
                for r in payload.get("providers") or []
                if isinstance(r, dict) and r.get("slug")
            ]
            payload["dotenv_configured_provider_slugs"] = (
                provider_slugs_configured_in_dotenv(row_slugs)
            )
        except Exception:
            payload["dotenv_configured_provider_slugs"] = []
        # Replace ai-gateway's curated 16-model subset with Vercel's
        # actual live catalog. ``hermes_cli.models.fetch_ai_gateway_models``
        # intersects Vercel's ``/v1/models`` response with a hard-coded
        # "recommended" list, so users see ~16 models when Vercel
        # actually exposes 100+. The curated list might be a reasonable
        # default in a terminal picker; in the extension's "All available
        # models" view it just looks broken ("I configured Vercel — why
        # is half of it missing?").
        await _expand_ai_gateway_models(payload)
        return web.json_response(payload)
    except Exception as exc:
        return json_error(500, f"failed to list model options: {exc}")


async def _expand_ai_gateway_models(payload: object) -> None:
    """Overwrite the ai-gateway row's ``models`` with Vercel's full live
    catalog when authenticated. Silently no-ops on any failure — the
    curated fallback Hermes provided stays in place.
    """
    import asyncio as _asyncio

    rows = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return
    target = None
    for r in rows:
        if isinstance(r, dict) and r.get("slug") == "ai-gateway":
            target = r
            break
    if target is None or not target.get("authenticated"):
        return
    full = await _asyncio.to_thread(_fetch_ai_gateway_full_catalog_sync)
    if not full:
        return
    target["models"] = full
    target["total_models"] = len(full)


_AI_GATEWAY_CATALOG_CACHE: tuple[float, list[dict]] | None = None
_AI_GATEWAY_CATALOG_TTL_S = 300.0


def _fetch_ai_gateway_full_catalog_sync() -> list[dict]:
    """Blocking helper: hit ``https://ai-gateway.vercel.sh/v1/models``
    (public, no auth needed for listing) and return rich entries
    ``{id, description?, metadata?}`` for every model. The extension's
    ``ModelEntryMetadataLine`` renders ``input_price_per_mtok`` /
    ``output_price_per_mtok`` automatically if metadata carries them,
    so we extract pricing here instead of letting it get stripped
    upstream.

    Cached for 5 min so the catalog endpoint stays cheap on repeated
    refreshes. Runs inside ``asyncio.to_thread`` so the event loop
    isn't blocked.
    """
    import json as _json
    import time as _time
    import urllib.request as _ur

    global _AI_GATEWAY_CATALOG_CACHE
    now = _time.monotonic()
    if _AI_GATEWAY_CATALOG_CACHE is not None:
        stamp, cached = _AI_GATEWAY_CATALOG_CACHE
        if now - stamp < _AI_GATEWAY_CATALOG_TTL_S:
            return [dict(e) for e in cached]
    try:
        from hermes_constants import AI_GATEWAY_BASE_URL  # type: ignore
    except Exception:
        AI_GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1"
    url = f"{str(AI_GATEWAY_BASE_URL).rstrip('/')}/models"
    try:
        req = _ur.Request(url, headers={"Accept": "application/json"})
        with _ur.urlopen(req, timeout=8.0) as resp:
            data = _json.loads(resp.read().decode())
    except Exception:
        return []
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        entry: dict = {"id": mid}
        meta: dict = {}

        # description: prefer Vercel's ``description`` (the prose blurb);
        # the existing UI renders ``entry.description`` in its own line
        # above the metadata chips. ``name`` is too short to deserve its
        # own slot — fold it in as a prefix on long blurbs and drop it
        # when the prose already starts with the name.
        prose = str(item.get("description") or "").strip()
        name = str(item.get("name") or "").strip()
        if prose:
            entry["description"] = prose
        elif name:
            entry["description"] = name

        pricing = item.get("pricing")
        if isinstance(pricing, dict):
            # Vercel's pricing is **per-token** USD strings:
            #   "0.00000012" = $0.12 per million tokens.
            # The UI's metadata renderer suffixes "$/M", so we multiply
            # by 1e6 and round before passing through. (Earlier revision
            # assumed per-million and shipped raw — rendered $0.00000012
            # /M, which is obviously broken.)
            ipt = _coerce_float(pricing.get("input"))
            opt = _coerce_float(pricing.get("output"))
            if ipt is not None and ipt > 0:
                meta["input_price_per_mtok"] = round(ipt * 1_000_000, 4)
            if opt is not None and opt > 0:
                meta["output_price_per_mtok"] = round(opt * 1_000_000, 4)
            # Tag free models (both prices exactly 0) so the existing
            # ``description: "free"`` UI affordance lights up. Override
            # any prose description — "free" is the salient signal.
            if (ipt is not None and ipt == 0) and (opt is not None and opt == 0):
                entry["description"] = "free"

        ctx_window = item.get("context_window") or item.get("context_length")
        if isinstance(ctx_window, (int, float)) and ctx_window > 0:
            meta["context_window"] = int(ctx_window)
        max_out = item.get("max_tokens") or item.get("max_output_tokens")
        if isinstance(max_out, (int, float)) and max_out > 0:
            meta["max_output_tokens"] = int(max_out)

        tags = item.get("tags")
        if isinstance(tags, list):
            clean_tags = [str(t).strip() for t in tags if str(t).strip()]
            if clean_tags:
                meta["tags"] = clean_tags

        if meta:
            entry["metadata"] = meta
        out.append(entry)
    if out:
        _AI_GATEWAY_CATALOG_CACHE = (now, [dict(e) for e in out])
    return out


def _coerce_float(value: object) -> float | None:
    """``float(value)`` swallowing TypeError/ValueError; returns None on
    failure. Used for permissive parsing of Vercel's stringified
    pricing fields.
    """
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None




def _user_configured_canonical_slugs(ctx) -> list[str]:
    """Canonical (alias-resolved) slugs of providers the user wrote in
    ``~/.hermes/config.yaml: providers:``.

    The raw ``user_providers`` dict on ``ctx`` keys by whatever the
    user typed (``vercel``, ``claude``, ``deep-seek``…). Run each
    through ``normalize_provider`` so the adapter can match them
    against the alias-resolved slugs that show up in row data
    (``ai-gateway``, ``anthropic``, ``deepseek``…).
    """
    try:
        from hermes_cli.models import normalize_provider  # type: ignore
    except Exception:
        return []
    raw = getattr(ctx, "user_providers", None) or {}
    if not isinstance(raw, dict):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for key in raw.keys():
        canon = normalize_provider(str(key))
        if canon and canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


# ---------------------------------------------------------------------------
# GET /hermes/provider-models — per-provider model list (mine-only)
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
# GET/POST /hermes/provider-credentials — plugin .env credentials (mine-only)
# ---------------------------------------------------------------------------


async def handle_provider_credentials_get(request: web.Request) -> web.Response:
    """Return ``{provider, keys, values}`` for one provider.

    ``keys`` is the env-var allow-list for the provider (from the
    Hermes profile registry); ``values`` is whatever's currently set
    in the plugin ``.env`` for those keys. Empty list when the
    provider has no registered credential keys (``auto``, OAuth-only
    providers, etc.).
    """
    provider = unquote(str(request.query.get("provider", ""))).strip()
    if not provider:
        return json_error(400, "missing provider query parameter")
    try:
        return web.json_response(read_provider_credentials_response(provider))
    except ValueError as exc:
        return json_error(400, str(exc))


async def handle_provider_credentials_post(request: web.Request) -> web.Response:
    """Merge values into plugin ``.env`` for one provider's allow-listed keys.

    Body::

        {"provider": "<slug>",
         "values":   {"OPENAI_API_KEY": "sk-…", …}}

    Only keys in the provider's allow-list are written; unknown keys
    are silently ignored (defence against UI bugs sending wrong keys
    to wrong providers). ``config.yaml`` is never touched by this
    endpoint — credentials are entirely .env-side.
    """
    try:
        body = await read_json_object(request)
    except web.HTTPBadRequest as exc:
        return exc
    provider = str(body.get("provider") or "").strip()
    if not provider:
        return json_error(400, "provider required")
    values = body.get("values")
    if values is None:
        values = {}
    if not isinstance(values, dict):
        return json_error(400, "values must be an object")
    try:
        written = merge_credentials_for_provider(provider, values)
    except ValueError as exc:
        return json_error(400, str(exc))
    return web.json_response(
        {"ok": True, "provider": provider, "written": written}
    )


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
                "/hermes/provider-credentials",
                handle_provider_credentials_get,
            ),
            web.post(
                "/hermes/provider-credentials",
                handle_provider_credentials_post,
            ),
        ]
    )
