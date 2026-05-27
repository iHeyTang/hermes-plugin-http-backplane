"""Provider slug → environment variable names (Hermes ``ProviderProfile.env_vars``)."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

from .dotenv_local import plugin_dotenv_path, read_dotenv_as_dict
from .hermes_core import get_provider_profile


# ---------------------------------------------------------------------------
# AI-intent override
# ---------------------------------------------------------------------------
#
# Some providers' ``ProviderProfile.env_vars`` tuples lump together env
# vars whose meanings spill outside "this provider as an AI source".
# The canonical example is Copilot:
#
#     env_vars = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
#
# ``GITHUB_TOKEN`` / ``GH_TOKEN`` are also used by Hermes' Skills Hub,
# Tirith security audits, and any tool that hits the GitHub REST API.
# A user who sets ``GITHUB_TOKEN`` to raise the GitHub API rate limit
# (5,000/hr instead of 60/hr) inadvertently flips Copilot to
# ``authenticated`` in our catalog, which then surfaces it as an AI
# provider they never intended to configure.
#
# This map narrows the "is this slug AI-configured by the user?" check
# to slug-specific env var names. The Hermes runtime keeps doing
# whatever it does (it will still happily route to Copilot when asked);
# we only stop *advertising* Copilot as configured-here unless the
# user explicitly set ``COPILOT_GITHUB_TOKEN``.
_AI_INTENT_ENV_OVERRIDES: Dict[str, Tuple[str, ...]] = {
    "copilot": ("COPILOT_GITHUB_TOKEN",),
}


def ai_intent_env_var_names_for_slug(slug: str) -> List[str]:
    """Subset of ``env_var_names_for_slug`` whose presence in ``.env``
    unambiguously means "the user configured this slug as an AI
    provider". For most slugs returns the full list; for slugs in
    ``_AI_INTENT_ENV_OVERRIDES`` returns only the slug-specific names.
    """
    s = str(slug).strip()
    full = env_var_names_for_slug(s)
    if not full:
        return []
    override = _AI_INTENT_ENV_OVERRIDES.get(s)
    if override is None:
        return full
    return [n for n in full if n in override]


def env_var_names_for_slug(slug: str) -> List[str]:
    """Return plugin-declared ``ProviderProfile.env_vars`` for a slug.

    This is the strict "is this provider configured" set — pluggable API
    keys only. URL override env vars (``DEEPSEEK_BASE_URL`` and friends)
    are NOT included here so that setting a stray BASE_URL in ``.env``
    can't falsely mark a provider as authenticated.
    """
    s = str(slug).strip()
    if not s or s in ("auto", "custom"):
        return []
    prof = get_provider_profile(s)
    if prof is None:
        return []
    ev: Any = getattr(prof, "env_vars", None)
    if not ev:
        return []
    return [str(x).strip() for x in ev if str(x).strip()]


def base_url_env_var_for_slug(slug: str) -> str:
    """Return the URL-override env var (e.g. ``DEEPSEEK_BASE_URL``) or ''.

    Sourced from ``hermes_cli.auth.PROVIDER_REGISTRY[slug].base_url_env_var``.
    Plugins don't typically include this in their ``env_vars`` tuple, so
    callers that want it (credentials UI) must read it separately.
    """
    s = str(slug).strip()
    if not s or s in ("auto", "custom"):
        return ""
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY  # type: ignore
    except Exception:
        return ""
    cfg = PROVIDER_REGISTRY.get(s)
    if cfg is None:
        return ""
    return str(getattr(cfg, "base_url_env_var", "") or "").strip()


def collect_provider_env_var_map(provider_slugs: List[str]) -> Dict[str, List[str]]:
    """Return ``{slug: ["API_KEY", ...]}`` for slugs that have a registered profile.

    Empty dict when no provider profiles can be resolved (e.g. bridge running
    without the full Hermes install).
    """
    out: Dict[str, List[str]] = {}
    seen: set[str] = set()
    for slug in provider_slugs:
        if slug in ("auto", "custom"):
            continue
        s = str(slug).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        names = env_var_names_for_slug(s)
        if names:
            out[s] = names
    return out


def provider_slugs_with_credentials_set(
    provider_env_map: Dict[str, List[str]],
) -> List[str]:
    """Return sorted slugs where at least one *AI-intent* env var is non-empty.

    Uses the bridge process environment (``~/.hermes/.env`` merged at
    startup / on save). Filters each slug's env var list through
    ``ai_intent_env_var_names_for_slug`` so overloaded vars like
    ``GITHUB_TOKEN`` — set by users who only want Skills Hub rate
    limits — don't falsely flag Copilot as configured.
    """
    out: List[str] = []
    for slug in sorted(provider_env_map.keys()):
        if slug in ("auto", "custom"):
            continue
        full_names = provider_env_map[slug]
        intent_names = ai_intent_env_var_names_for_slug(slug)
        # Fall back to the full list when the slug has no override (most
        # providers) — ``ai_intent_env_var_names_for_slug`` already
        # returns the full list in that case, but ``provider_env_map``
        # may have been hand-built with a different set, so we trust
        # the caller's list shape and only narrow when the override
        # applies.
        names = (
            [n for n in full_names if n in intent_names]
            if slug in _AI_INTENT_ENV_OVERRIDES
            else full_names
        )
        if any(str(os.environ.get(n, "") or "").strip() for n in names):
            out.append(slug)
    return out


def provider_slugs_configured_in_dotenv(slugs: List[str]) -> List[str]:
    """Strict "user configured this slug *as an AI provider* here" set.

    Reads ``~/.hermes/.env`` directly (NOT ``os.environ``) so ambient
    credentials Hermes detects elsewhere — shell env vars set by the
    host, Claude Code's ``~/.claude/...`` OAuth tokens, ``gh`` CLI auth,
    AWS SDK config, etc. — don't get conflated with what the user
    actually saved through the extension's "Save credentials" button.

    Uses ``ai_intent_env_var_names_for_slug`` instead of the full
    profile env var list, so generic tokens with multiple purposes
    (the classic case: ``GITHUB_TOKEN`` is *also* used for Skills Hub
    rate limits and other GitHub API operations, but its profile lists
    it under Copilot's ``env_vars``) don't falsely flag the provider
    as AI-configured.
    """
    dotenv = read_dotenv_as_dict(plugin_dotenv_path())
    if not dotenv:
        return []
    out: List[str] = []
    seen: set[str] = set()
    for slug in slugs:
        s = str(slug).strip()
        if not s or s in ("auto", "custom") or s in seen:
            continue
        seen.add(s)
        names = ai_intent_env_var_names_for_slug(s)
        if not names:
            continue
        if any(str(dotenv.get(n, "") or "").strip() for n in names):
            out.append(s)
    return out
