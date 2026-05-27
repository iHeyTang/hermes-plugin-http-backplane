"""HTTP reverse proxy for ``/v1/*`` → Hermes gateway (port 8642).

Bridges the chat-side OpenAI-compatible surface (chat/completions,
runs, models, approval) into the backplane so callers only ever have
to know one host + one key. From the extension's point of view there
is no "Hermes API server" — just the backplane.

Auth handling
-------------

- Inbound: the backplane's ``HERMES_BACKPLANE_KEY`` middleware has
  already validated by the time we get here. We **drop** the inbound
  ``Authorization`` header (it's the backplane key, not the gateway
  key — different trust domain).
- Outbound: if ``API_SERVER_KEY`` is set in the environment we attach
  it as ``Bearer`` on the upstream request. The user never sees this;
  the env var is Hermes-core's concern.

Streaming
---------

``POST /v1/chat/completions`` and ``GET /v1/runs/{id}/events`` are
SSE — we pipe the upstream body chunk-by-chunk via
``StreamResponse`` so token deltas don't get buffered. Everything
else is buffered (small JSON).
"""

from __future__ import annotations

from . import routes


def register(app):
    routes.register(app)
