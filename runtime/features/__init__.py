"""Feature modules that compose the backplane's HTTP surface.

Two lanes:

- ``hermes_proxy`` — thin HTTP wrappers over Hermes core APIs (cron,
  sessions, model catalog, provider settings, memory, skills, attachment
  uploads) that the gateway doesn't expose itself. Mounted at
  ``/hermes/*``. Retired piecemeal as the gateway grows native
  equivalents.
- ``integrations`` — agent-managed endpoints served under
  ``/integrations/<name>/*``. The loader (``integrations.loader``)
  discovers built-in presets plus user integrations under
  ``~/.hermes/integrations/`` and registers each via
  :func:`runtime.api.register_integration`. The actual per-request
  routing is done by the catch-all dispatcher in
  :mod:`runtime.dispatch`.
"""

from __future__ import annotations

from aiohttp import web

from . import hermes_proxy


def register_native(app: web.Application) -> None:
    """Register the native hermes_proxy lane."""
    hermes_proxy.register(app)
