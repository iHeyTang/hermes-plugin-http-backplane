"""
Per-provider model lists — composes upstream curated lists, manifest entries
and live pricing into a unified response.

This is a pure composition service: it has no direct external I/O of its own
and depends on ``adapters.hermes_core`` for any upstream calls.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from ....adapters.hermes_core import (
    curated_models_for_provider,
    get_pricing_for_provider,
    hermes_cli_models_available,
    normalize_provider,
)

logger = logging.getLogger("my-browser-bridge")

_TOP_LEVEL_MODEL_META_KEYS = (
    "context_window",
    "max_context_tokens",
    "max_output_tokens",
    "max_tokens",
    "input_price_per_mtok",
    "output_price_per_mtok",
    "input_price",
    "output_price",
    "pricing",
    "pricing_tier",
    "modality",
    "modalities",
    "parameters",
)


def _json_safe_meta_scalar(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool)) or v is None


def _sanitize_metadata_dict(d: Any) -> Dict[str, Any]:
    if not isinstance(d, dict):
        return {}
    out: Dict[str, Any] = {}
    for k, v in d.items():
        if not isinstance(k, str) or not k.strip():
            continue
        ks = k.strip()
        if _json_safe_meta_scalar(v):
            out[ks] = v
        elif (
            isinstance(v, list)
            and len(v) <= 32
            and all(_json_safe_meta_scalar(x) for x in v)
        ):
            out[ks] = list(v)
    return out


def _entry_metadata_from_manifest_row(m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    merged: Dict[str, Any] = {}
    nested = m.get("metadata")
    if isinstance(nested, dict):
        merged.update(_sanitize_metadata_dict(nested))
    for key in _TOP_LEVEL_MODEL_META_KEYS:
        if key not in m:
            continue
        val = m[key]
        if _json_safe_meta_scalar(val):
            merged[key] = val
        elif (
            isinstance(val, list)
            and len(val) <= 32
            and all(_json_safe_meta_scalar(x) for x in val)
        ):
            merged[key] = list(val)
    return merged if merged else None


def _manifest_models_by_id(block: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not block or not isinstance(block.get("models"), list):
        return out
    for m in block["models"]:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if isinstance(mid, str) and mid.strip():
            out[mid.strip()] = m
    return out


def _manifest_block_for_provider(
    manifest: Optional[Dict[str, Any]], *keys: str
) -> Optional[Dict[str, Any]]:
    if not manifest or not isinstance(manifest.get("providers"), dict):
        return None
    provs = manifest["providers"]
    for k in keys:
        if not k:
            continue
        block = provs.get(k)
        if isinstance(block, dict):
            return block
    return None


def _desc_overlay_from_manifest(block: Optional[Dict[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not block or not isinstance(block.get("models"), list):
        return out
    for m in block["models"]:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not isinstance(mid, str) or not mid.strip():
            continue
        d = m.get("description")
        if isinstance(d, str) and d.strip():
            out[mid.strip()] = d.strip()
    return out


def _models_from_manifest_block(block: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not block or not isinstance(block.get("models"), list):
        return out
    for m in block["models"]:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not isinstance(mid, str) or not mid.strip():
            continue
        desc = m.get("description")
        entry: Dict[str, Any] = {"id": mid.strip()}
        if isinstance(desc, str) and desc.strip():
            entry["description"] = desc.strip()
        meta = _entry_metadata_from_manifest_row(m)
        if meta:
            entry["metadata"] = meta
        out.append(entry)
    return out


def _price_per_million_from_openrouter_token_price(s: Any) -> Optional[float]:
    if s is None:
        return None
    t = str(s).strip()
    if not t:
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return round(v * 1_000_000.0, 10)


def _merge_live_pricing_into_entry(
    entry: Dict[str, Any], pricing_map: Dict[str, Dict[str, str]]
) -> None:
    mid = entry.get("id")
    if not isinstance(mid, str) or not mid.strip():
        return
    row = pricing_map.get(mid.strip())
    if not row:
        return
    meta: Dict[str, Any] = dict(entry.get("metadata") or {})
    if "input_price_per_mtok" not in meta:
        ip = _price_per_million_from_openrouter_token_price(row.get("prompt"))
        if ip is not None:
            meta["input_price_per_mtok"] = ip
    if "output_price_per_mtok" not in meta:
        op = _price_per_million_from_openrouter_token_price(row.get("completion"))
        if op is not None:
            meta["output_price_per_mtok"] = op
    if meta:
        entry["metadata"] = meta


def _apply_pricing_to_models(
    models: List[Dict[str, Any]], pricing_map: Dict[str, Dict[str, str]]
) -> None:
    for e in models:
        _merge_live_pricing_into_entry(e, pricing_map)


def _fetch_pricing_map(
    provider_raw: str, *, force_refresh: bool
) -> Dict[str, Dict[str, str]]:
    norm = normalize_provider(provider_raw)
    return get_pricing_for_provider(norm, force_refresh=force_refresh)


def build_provider_models_response(
    provider: str,
    *,
    manifest: Optional[Dict[str, Any]] = None,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    raw = (provider or "").strip()
    if not raw or raw == "auto":
        return {
            "ok": True,
            "provider": raw,
            "models": [],
            "source": "skipped",
            "cli_loaded": False,
            "pricing_loaded": False,
        }

    cli_loaded = hermes_cli_models_available()
    normalized = normalize_provider(raw)
    cli_tuples: List[Tuple[str, str]] = curated_models_for_provider(
        normalized, force_refresh=force_refresh
    )

    pricing_map = _fetch_pricing_map(raw, force_refresh=force_refresh)
    pricing_loaded = bool(pricing_map)

    block = _manifest_block_for_provider(manifest, normalized, raw)
    desc_overlay = _desc_overlay_from_manifest(block)
    manifest_rows = _manifest_models_by_id(block)

    if cli_tuples:
        models: List[Dict[str, Any]] = []
        for mid, desc in cli_tuples:
            m = str(mid).strip()
            if not m:
                continue
            merged = desc_overlay.get(m, (desc or "").strip())
            entry: Dict[str, Any] = {"id": m}
            if merged:
                entry["description"] = merged
            row = manifest_rows.get(m)
            if row:
                meta = _entry_metadata_from_manifest_row(row)
                if meta:
                    entry["metadata"] = meta
            models.append(entry)
        _apply_pricing_to_models(models, pricing_map)
        return {
            "ok": True,
            "provider": raw,
            "models": models,
            "source": "hermes_cli",
            "cli_loaded": cli_loaded,
            "pricing_loaded": pricing_loaded,
        }

    mf_models = _models_from_manifest_block(block)
    if mf_models:
        _apply_pricing_to_models(mf_models, pricing_map)
        return {
            "ok": True,
            "provider": raw,
            "models": mf_models,
            "source": "manifest",
            "cli_loaded": cli_loaded,
            "pricing_loaded": pricing_loaded,
        }

    return {
        "ok": True,
        "provider": raw,
        "models": [],
        "source": "none",
        "cli_loaded": cli_loaded,
        "pricing_loaded": pricing_loaded,
    }
