"""HTTP admin endpoints for ``/integrations/<name>/*`` lifecycle.

These are **not** agent capabilities. They exist so the
``hermes integration`` CLI subcommand (which may run in a separate
``hermes`` invocation) can ask the live backplane to re-register or
unregister an integration after it has changed the files under
``~/.hermes/integrations/<name>/``.

Loopback-only — the backplane listens on ``127.0.0.1`` and there is
no auth, same threat model as the rest of ``/hermes/*``.
"""

from __future__ import annotations

from .routes import register

__all__ = ["register"]
