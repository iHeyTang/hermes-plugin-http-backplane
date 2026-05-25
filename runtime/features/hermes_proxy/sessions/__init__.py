"""HTTP wrapper around Hermes core's ``hermes_state.SessionDB``.

Hermes already owns session storage (``state.db``, sqlite + FTS5 + WAL).
We expose a subset of that surface at ``/hermes/sessions/*`` so the
browser extension (and any future client) can replace its private
``chrome.storage.local`` history with the canonical Hermes log without
requiring the user to run ``hermes dashboard`` — the dashboard's HTTP
layer is a separate uvicorn process that's optional and may not be up.

This module reads SessionDB directly. SessionDB is designed for shared
access across multiple Hermes processes (WAL mode + jittered retry on
``database is locked``), so opening a connection from the backplane
thread is safe alongside the gateway, CLI, dashboard, etc.

Standalone-extractable: depends only on Hermes core's ``hermes_state``
module and ``$HERMES_HOME/state.db``.
"""

from __future__ import annotations

from .routes import register

__all__ = ["register"]
