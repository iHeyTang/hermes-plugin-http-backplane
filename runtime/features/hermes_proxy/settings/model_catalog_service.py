from __future__ import annotations

import logging
from typing import Any, Dict

from ....adapters.hermes_agent_model import read_config_provider_keys
from ....adapters.hermes_core import load_canonical_providers
from ....adapters.hermes_model_catalog import get_model_catalog_manifest, merge_provider_ids
from ....adapters.hermes_provider_env import (
    collect_provider_env_var_map,
    provider_slugs_with_credentials_set,
)
from .provider_models_service import build_provider_models_response

logger = logging.getLogger("my-browser-bridge")


def _has_models_block(block: Any) -> bool:
    return (
        isinstance(block, dict)
        and isinstance(block.get("models"), list)
        and len(block["models"]) > 0
    )


def build_model_catalog_response(*, force_refresh: bool) -> Dict[str, Any]:
    manifest, cat_source = get_model_catalog_manifest(force_refresh=force_refresh)
    cfg_prov_keys = read_config_provider_keys()
    canonical = load_canonical_providers()
    canonical_slugs = (
        [str(p["slug"]) for p in canonical if p.get("slug")] if canonical else None
    )
    merged_ids = merge_provider_ids(manifest, cfg_prov_keys, canonical_slugs)
    provider_env_vars = collect_provider_env_var_map(merged_ids)
    env_ready = provider_slugs_with_credentials_set(provider_env_vars)

    providers_body: Dict[str, Any] = {}
    if manifest and isinstance(manifest.get("providers"), dict):
        providers_body = dict(manifest["providers"])

    for slug in env_ready:
        cur = providers_body.get(slug)
        if _has_models_block(cur):
            continue
        try:
            resp = build_provider_models_response(
                slug, manifest=manifest, force_refresh=force_refresh
            )
            model_list = resp.get("models")
            if not isinstance(model_list, list) or not model_list:
                continue
            base: Dict[str, Any] = {}
            if isinstance(cur, dict) and isinstance(cur.get("metadata"), dict):
                base["metadata"] = cur["metadata"]
            providers_body[slug] = {**base, "models": model_list}
        except Exception as exc:
            logger.info("catalog model enrich for %r skipped: %s", slug, exc)

    body: Dict[str, Any] = {
        "ok": True,
        "catalog_source": cat_source,
        "updated_at": manifest.get("updated_at") if manifest else None,
        "metadata": manifest.get("metadata") if manifest else None,
        "providers": providers_body,
        "provider_ids": merged_ids,
        "config_provider_ids": cfg_prov_keys,
        "env_ready_provider_ids": env_ready,
        "canonical_providers": canonical or [],
        "canonical_loaded": bool(canonical),
        "provider_env_vars": provider_env_vars,
    }
    if manifest is None:
        body["warning"] = (
            "Could not load catalog: check network or run `hermes model` once so "
            "~/.hermes/cache/model_catalog.json exists."
        )
    return body


def build_provider_models_http_response(
    *, provider: str, force_refresh: bool
) -> Dict[str, Any]:
    manifest, _source = get_model_catalog_manifest(force_refresh=force_refresh)
    return build_provider_models_response(
        provider, manifest=manifest, force_refresh=force_refresh
    )

