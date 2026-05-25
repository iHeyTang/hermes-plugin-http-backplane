"""
Single integration point with the Hermes core Python package.

All upstream symbol lookups, optional-import fallbacks, and HERMES_HOME
resolution live here. Other adapters MUST go through this module instead
of importing ``hermes_constants`` / ``hermes_cli`` / ``providers`` directly,
so that upstream changes only require edits in one place.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("my-browser-bridge")

_FALLBACK_HERMES_HOME = Path.home() / ".hermes"


@lru_cache(maxsize=1)
def hermes_home() -> Path:
    """Resolve the Hermes HOME directory.

    Prefers ``hermes_constants.get_hermes_home`` when the core package is
    importable; otherwise falls back to ``~/.hermes``.
    """
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:
        return _FALLBACK_HERMES_HOME


@lru_cache(maxsize=1)
def hermes_cli_models_available() -> bool:
    """Whether the ``hermes_cli.models`` module is importable in this process."""
    try:
        import hermes_cli.models  # type: ignore  # noqa: F401

        return True
    except Exception:
        return False


def load_canonical_providers() -> Optional[List[Dict[str, Any]]]:
    """Return canonical provider rows ``[{slug, label, tui_desc}, ...]``.

    Returns ``None`` when the upstream package is unavailable.
    """
    try:
        from hermes_cli.models import CANONICAL_PROVIDERS  # type: ignore
    except Exception:
        return None

    out: List[Dict[str, Any]] = []
    try:
        for p in CANONICAL_PROVIDERS:
            slug = str(getattr(p, "slug", "") or "").strip()
            if not slug:
                continue
            label = str(getattr(p, "label", "") or slug)
            out.append(
                {
                    "slug": slug,
                    "label": label,
                    "tui_desc": str(getattr(p, "tui_desc", "") or label),
                }
            )
    except Exception:
        return None
    return out


def normalize_provider(raw: str) -> str:
    """Normalize a provider slug via Hermes CLI; return ``raw`` on failure."""
    try:
        from hermes_cli.models import normalize_provider as _normalize  # type: ignore

        return _normalize(raw)
    except Exception:
        return raw


def curated_models_for_provider(
    normalized: str, *, force_refresh: bool = False
) -> List[Tuple[str, str]]:
    """Return curated ``(id, description)`` tuples; empty list on failure."""
    try:
        from hermes_cli.models import curated_models_for_provider as _curated  # type: ignore

        return list(_curated(normalized, force_refresh=force_refresh))
    except Exception as exc:
        logger.info("hermes_cli curated_models(%r) failed: %s", normalized, exc)
        return []


def get_pricing_for_provider(
    normalized: str, *, force_refresh: bool = False
) -> Dict[str, Dict[str, str]]:
    """Return live pricing map for the provider; empty dict on failure."""
    try:
        from hermes_cli.models import get_pricing_for_provider as _pricing  # type: ignore

        return _pricing(normalized, force_refresh=force_refresh)
    except Exception as exc:
        logger.info("hermes_cli pricing(%r) failed: %s", normalized, exc)
        return {}


def get_provider_profile(slug: str) -> Optional[Any]:
    """Return the upstream ``ProviderProfile`` for ``slug``, or ``None``.

    Wraps ``providers.get_provider_profile`` so callers don't need to handle
    the optional import themselves.
    """
    try:
        from providers import get_provider_profile as _get  # type: ignore

        return _get(slug)
    except Exception:
        return None
