"""Hermes core settings — HTTP shims over Hermes Agent's own config surfaces.

Four logically distinct read/write surfaces, grouped here because they
all wrap Hermes core state (``~/.hermes/config.yaml`` + adapters) and
share no client UI other than the options page:

- **Models** — provider catalog, per-provider model lists, main and
  auxiliary model selection (``model_routes`` + ``config_routes``).
- **Providers** — provider credentials in the plugin's ``.env``
  (``config_routes`` POST, ``provider_credentials_service``).
- **Skills** — Hermes skill discovery, file browser, enable/disable
  toggle (``skills_routes``).
- **Memory** — read-only view of curated ``MEMORY.md`` / ``USER.md``
  with the upstream threat scanner's verdict per entry (``memory_routes``).

Each sub-domain could later split into its own plugin; today they share
adapter imports (``hermes_agent_model``, ``hermes_core``, …) and the
umbrella keeps the wiring concise.
"""

from __future__ import annotations

from aiohttp import web

from .config_routes import register_config_routes
from .memory_routes import register_memory_routes
from .model_routes import register_model_routes
from .skills_routes import register_skills_routes


def register(app: web.Application) -> None:
    register_model_routes(app)
    register_config_routes(app)
    register_memory_routes(app)
    register_skills_routes(app)


__all__ = ["register"]
