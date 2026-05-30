"""Conversation attachment upload/delete surface.

Sits under ``/hermes/attachments/*`` because attaching files is a
conversation-level concern (alongside ``/hermes/sessions/*/messages``),
not something specific to any particular client. The Hermes browser
extension is the first caller, but desktop/CLI/mobile clients would
hit the same endpoints.

Persists uploads under
``<hermes_home>/hermes-x/inbox/<session>/``.
"""

from __future__ import annotations

from .routes import max_client_size_bytes, register

__all__ = ["register", "max_client_size_bytes"]
