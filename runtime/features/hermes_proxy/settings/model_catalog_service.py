"""Catalog helper used by the mine-only ``/hermes/provider-models``.

The big multi-provider catalog endpoint (``/hermes/model/options``) now
delegates strictly to ``hermes_cli.inventory.build_models_payload`` —
the local-fallback path was removed for parity with upstream, taking
the bulk of this module with it. Only the per-provider lookup that
backs the (mine-only) ``GET /hermes/provider-models`` endpoint remains.
"""

from __future__ import annotations

from typing import Any, Dict

from ....adapters.hermes_model_catalog import get_model_catalog_manifest
from .provider_models_service import build_provider_models_response


def build_provider_models_http_response(
    *, provider: str, force_refresh: bool
) -> Dict[str, Any]:
    manifest, _source = get_model_catalog_manifest(force_refresh=force_refresh)
    return build_provider_models_response(
        provider, manifest=manifest, force_refresh=force_refresh
    )

