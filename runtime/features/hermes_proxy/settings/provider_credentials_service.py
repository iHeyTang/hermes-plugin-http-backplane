"""Read plugin credential keys scoped by Hermes provider profile."""

from __future__ import annotations

from typing import Any, Dict, List

from ....adapters.dotenv_local import (
    get_dotenv_values_for_keys,
    is_valid_env_key,
    merge_dotenv_file_and_apply,
)
from ....adapters.hermes_provider_env import env_var_names_for_slug

# Matches extension ``HermesModelConfigTab`` custom-provider UX.
_CUSTOM_PROVIDER_ENV_KEYS = (
    "CUSTOM_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
)


def allowed_credential_keys_for_provider(provider: str) -> List[str]:
    s = str(provider or "").strip()
    if not s or s == "auto":
        return []
    if s == "custom":
        return list(_CUSTOM_PROVIDER_ENV_KEYS)
    return env_var_names_for_slug(s)


def read_provider_credentials_response(provider: str) -> Dict[str, Any]:
    raw = str(provider or "").strip()
    if not raw:
        raise ValueError("missing provider query parameter")
    keys = allowed_credential_keys_for_provider(raw)
    if not keys:
        return {"ok": True, "provider": raw, "keys": [], "values": {}}
    vals = get_dotenv_values_for_keys(keys)
    return {"ok": True, "provider": raw, "keys": keys, "values": vals}


def merge_credentials_for_provider(provider: str, values: Dict[str, Any]) -> List[str]:
    """Merge *values* into the plugin ``.env`` for *provider*'s allowed keys only.

    No-op (returns ``[]``) when *provider* is empty, ``auto``, or has no registered
    credential keys. Unknown keys in *values* are ignored.
    """
    raw = str(provider or "").strip()
    if not raw or raw == "auto":
        return []
    allowed = allowed_credential_keys_for_provider(raw)
    if not allowed:
        return []
    allowed_set = set(allowed)
    updates: Dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or key not in allowed_set:
            continue
        if not is_valid_env_key(key):
            continue
        if value is not None and not isinstance(value, str):
            raise ValueError(f"value for {key!r} must be string")
        updates[key] = str(value) if value is not None else ""

    merge_dotenv_file_and_apply(updates)
    return sorted(updates.keys())
