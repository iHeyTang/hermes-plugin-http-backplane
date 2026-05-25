"""
Hermes model catalog from official docs JSON + local disk cache fallback.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .hermes_core import hermes_home

logger = logging.getLogger("my-browser-bridge")

DEFAULT_CATALOG_URL = "https://hermes-agent.nousresearch.com/docs/api/model-catalog.json"
BRIDGE_USER_AGENT = "hermes-my-browser-extension-bridge/1.0"
SUPPORTED_SCHEMA_VERSION = 1
DEFAULT_FETCH_TIMEOUT = 15.0


def _disk_cache_path() -> Path:
    return hermes_home() / "cache" / "model_catalog.json"


def _validate_manifest(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    version = data.get("version")
    if not isinstance(version, int) or version > SUPPORTED_SCHEMA_VERSION:
        return False
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return False
    for pname, pblock in providers.items():
        if not isinstance(pname, str) or not isinstance(pblock, dict):
            return False
        models = pblock.get("models")
        if not isinstance(models, list):
            return False
        for m in models:
            if not isinstance(m, dict):
                return False
            if not isinstance(m.get("id"), str) or not m["id"].strip():
                return False
    return True


def _fetch_url(url: str, timeout: float) -> Optional[Dict[str, Any]]:
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": BRIDGE_USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        logger.info("model catalog fetch failed: %s", exc)
        return None
    if not _validate_manifest(data):
        logger.info("model catalog failed validation")
        return None
    return data


def _read_disk_cache() -> Optional[Dict[str, Any]]:
    path = _disk_cache_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not _validate_manifest(data):
        return None
    return data


def get_model_catalog_manifest(
    *, force_refresh: bool = False
) -> Tuple[Optional[Dict[str, Any]], str]:
    if force_refresh:
        fetched = _fetch_url(DEFAULT_CATALOG_URL, DEFAULT_FETCH_TIMEOUT)
        if fetched is not None:
            return fetched, "remote"
        disk = _read_disk_cache()
        if disk is not None:
            return disk, "disk_cache"
        return None, "none"

    disk = _read_disk_cache()
    if disk is not None:
        return disk, "disk_cache"

    fetched = _fetch_url(DEFAULT_CATALOG_URL, DEFAULT_FETCH_TIMEOUT)
    if fetched is not None:
        return fetched, "remote"

    return None, "none"


def merge_provider_ids(
    manifest: Optional[Dict[str, Any]],
    config_provider_keys: List[str],
    canonical_slugs: Optional[List[str]] = None,
) -> List[str]:
    """Provider dropdown ordering."""
    seen: set[str] = set()
    out: List[str] = []

    def add(pid: str) -> None:
        pid = str(pid).strip()
        if not pid or pid in seen:
            return
        seen.add(pid)
        out.append(pid)

    if canonical_slugs:
        add("auto")
        for s in canonical_slugs:
            add(s)
        if manifest and isinstance(manifest.get("providers"), dict):
            for k in sorted(manifest["providers"].keys()):
                if isinstance(k, str):
                    add(k)
        for k in sorted(config_provider_keys):
            if isinstance(k, str):
                add(k)
        add("custom")
        return out

    for b in ("auto", "custom"):
        add(b)
    if manifest and isinstance(manifest.get("providers"), dict):
        for k in sorted(manifest["providers"].keys()):
            if isinstance(k, str):
                add(k)
    for k in sorted(config_provider_keys):
        if isinstance(k, str):
            add(k)
    return out

