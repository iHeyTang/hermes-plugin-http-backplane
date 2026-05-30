"""HTTP wrappers around Hermes core APIs the gateway doesn't expose.

Sub-modules:
- ``cron``: ``/hermes/cron/*`` — wraps Hermes's cron module
- ``settings``: ``/hermes/model/info``, ``/hermes/model/auxiliary``,
  ``/hermes/model/options``, ``/hermes/model/set``,
  ``/hermes/provider-models``, ``/hermes/provider-credentials``,
  ``/hermes/memories``, ``/hermes/skills``
- ``sessions``: ``/hermes/sessions/*`` — read-only view over
  ``hermes_state.SessionDB`` (the canonical conversation log)
- ``attachments``: ``/hermes/attachments*`` — upload/delete conversation
  attachments, persisted under
  ``<hermes_home>/hermes-x/inbox/<session>/``
- ``integrations_admin``: ``/hermes/integrations*`` — lifecycle admin
  for ``/integrations/<name>/*``; called by the ``hermes integration``
  CLI subcommand, NOT exposed as agent tools

These wrap ``hermes_state`` / ``cron.jobs`` etc. as Python libraries
directly. That makes them available whenever the backplane is loaded —
unlike the dashboard's ``/api/*`` FastAPI app, which only runs while
the user has ``hermes dashboard`` open. When the gateway grows native
HTTP routes for these, the proxy modules become redundant and can be
retired.
"""

from __future__ import annotations

from aiohttp import web

from . import (
    attachments,
    cron,
    integrations_admin,
    lifecycle,
    logs,
    sessions,
    settings,
)


def register(app: web.Application) -> None:
    """Register all hermes_proxy routes onto *app*."""
    cron.register(app)
    settings.register(app)
    sessions.register(app)
    attachments.register(app)
    integrations_admin.register(app)
    lifecycle.register(app)
    logs.register(app)
