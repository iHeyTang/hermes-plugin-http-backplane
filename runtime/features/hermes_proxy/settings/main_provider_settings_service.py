"""Read and save main Hermes model + plugin credentials as one business surface."""

from __future__ import annotations

from typing import Any, Dict, Optional

from ....adapters.hermes_agent_model import read_main_model, write_main_model
from .provider_credentials_service import (
    merge_credentials_for_provider,
    read_provider_credentials_response,
)


def read_main_provider_settings_response(
    *, credentials_for: Optional[str] = None
) -> Dict[str, Any]:
    """Return saved main model block plus credential keys/values for one provider slug.

    *credentials_for* (query ``provider``): which provider's env keys to load
    (sidebar selection). When omitted or empty, uses the provider currently saved
    in ``config.yaml`` so one GET matches the persisted main-model row.
    """
    main = read_main_model()
    out: Dict[str, Any] = {"ok": True, **main}

    slug = (credentials_for or "").strip()
    if not slug:
        slug = str(main.get("provider") or "auto").strip() or "auto"
    if not slug:
        slug = "auto"

    try:
        cred = read_provider_credentials_response(slug)
    except ValueError:
        cred = {"ok": True, "provider": slug, "keys": [], "values": {}}

    out["credentials"] = {
        "provider": cred.get("provider", slug),
        "keys": list(cred.get("keys") or []),
        "values": dict(cred.get("values") or {}),
    }
    return out


def save_main_provider_settings_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Apply plugin ``.env`` credential updates (optional), then ``config.yaml`` model block.

    When the JSON body includes a ``credentials`` key and its value is not
    ``null``, it must be an object; only keys allowed for ``provider`` are
    written. Omit ``credentials`` entirely to leave ``.env`` unchanged.
    """
    if "credentials" in payload:
        cred = payload["credentials"]
        if cred is not None:
            if not isinstance(cred, dict):
                raise ValueError("credentials must be an object or null")
            merge_credentials_for_provider(str(payload.get("provider") or ""), cred)

    merged = write_main_model(
        provider=payload.get("provider"),
        model=payload.get("model"),
        base_url=payload.get("base_url"),
    )
    return {"ok": True, **merged}
