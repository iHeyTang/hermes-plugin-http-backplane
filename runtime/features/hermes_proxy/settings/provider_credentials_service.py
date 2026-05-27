"""Per-provider credential schema — what env vars to render + their current values.

Each provider profile self-declares which env vars matter via
``ProviderProfile.env_vars``. We surface those plus, for each one, a
placeholder when there's a meaningful default (URL fields get the
provider's stock ``base_url``).

OAuth-only providers (``env_vars=()``) return an empty ``fields`` list
and an ``auth_hint`` describing how the user authenticates.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ....adapters.dotenv_local import (
    get_dotenv_values_for_keys,
    is_valid_env_key,
    merge_dotenv_file_and_apply,
)
from ....adapters.hermes_core import get_provider_profile
from ....adapters.hermes_provider_env import (
    base_url_env_var_for_slug,
    env_var_names_for_slug,
)

# Matches the extension's custom-provider UX: any of these env vars is
# accepted as the bearer token for a user-supplied OpenAI-compatible
# endpoint.
_CUSTOM_PROVIDER_ENV_KEYS = (
    "CUSTOM_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
)


def _provider_default_base_url(slug: str) -> str:
    prof = get_provider_profile(slug)
    if prof is None:
        return ""
    bu = getattr(prof, "base_url", "") or ""
    return str(bu).strip()


def _field_kind(key: str) -> str:
    """Coarse field-kind hint for the UI.

    - ``url``: the key looks URL-shaped (``*_BASE_URL`` / ``*_URL``)
    - ``secret``: everything else (rendered as a password / API-key input)
    """
    up = key.upper()
    if up.endswith("_BASE_URL") or up.endswith("_URL"):
        return "url"
    return "secret"


def _field_placeholder(key: str, default_base_url: str) -> str:
    if _field_kind(key) == "url":
        return default_base_url
    return ""


def allowed_credential_keys_for_provider(slug: str) -> List[str]:
    """Env var allow-list for read/write — all writable vars for the slug.

    Composes plugin-declared API key env vars with the canonical URL
    override env var (``DEEPSEEK_BASE_URL``, ``OPENAI_BASE_URL``, …) when
    one exists. The URL var lives in ``hermes_cli.auth.PROVIDER_REGISTRY``
    and is rarely mirrored in the plugin's ``env_vars`` tuple, so we
    merge here to give the credentials panel a complete view.
    """
    s = str(slug or "").strip()
    if not s or s == "auto":
        return []
    if s == "custom":
        return list(_CUSTOM_PROVIDER_ENV_KEYS)
    keys: List[str] = list(env_var_names_for_slug(s))
    seen = set(keys)
    url_env = base_url_env_var_for_slug(s)
    if url_env and url_env not in seen:
        keys.append(url_env)
    return keys


# Plain-language hint per auth_type, shown when the provider has no
# env-editable fields. Empty string means "show nothing".
_AUTH_HINT_BY_TYPE = {
    "oauth_device_code": "Sign in with `hermes auth login {slug}`.",
    "oauth_external": (
        "Authenticate via the external CLI (Claude Code / Codex / Qwen / etc.)."
    ),
    "external_process": "Started and authenticated by an external process.",
    "aws_sdk": "Uses AWS SDK credentials from `~/.aws` or the IAM environment.",
    "copilot": "Uses your GitHub Copilot subscription.",
}


def _auth_hint_for(slug: str) -> str:
    """Short hint shown when a provider has nothing to fill in here."""
    prof = get_provider_profile(slug)
    if prof is None:
        return ""
    auth_type = getattr(prof, "auth_type", "api_key")
    tmpl = _AUTH_HINT_BY_TYPE.get(auth_type, "")
    return tmpl.format(slug=slug) if tmpl else ""


def read_provider_credentials_response(provider: str) -> Dict[str, Any]:
    raw = str(provider or "").strip()
    if not raw:
        raise ValueError("missing provider query parameter")
    keys = allowed_credential_keys_for_provider(raw)
    if not keys:
        return {
            "ok": True,
            "provider": raw,
            "fields": [],
            "auth_hint": _auth_hint_for(raw),
        }
    default_base_url = _provider_default_base_url(raw)
    values = get_dotenv_values_for_keys(keys)
    fields: List[Dict[str, str]] = []
    for k in keys:
        fields.append(
            {
                "key": k,
                "value": values.get(k, ""),
                "placeholder": _field_placeholder(k, default_base_url),
                "kind": _field_kind(k),
            }
        )
    return {
        "ok": True,
        "provider": raw,
        "fields": fields,
        "auth_hint": "",
    }


def merge_credentials_for_provider(provider: str, values: Dict[str, Any]) -> List[str]:
    """Merge *values* into the plugin ``.env`` for *provider*'s allowed keys only.

    No-op (returns ``[]``) when *provider* is empty, ``auto``, or has no
    registered credential keys. Unknown keys in *values* are ignored
    (defence against UI bugs sending wrong keys to wrong providers).
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
