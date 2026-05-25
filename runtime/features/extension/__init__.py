"""Chrome-extension companion HTTP surface.

The WebSocket hub (see ``bridge/server.py``) is the actual relay between
the Hermes plugin and the Chrome extension. This feature carries the
HTTP-side pieces that belong to the same domain:

- ``POST /attach`` — side-panel attachment uploads, persisted under
  ``<HERMES_HOME>/plugins/<plugin>/attachments/<session>/``.

Standalone-extractable: would become its own plugin in the same package
as ``server.py`` since the two share the same WebSocket contract.
"""

from __future__ import annotations

from .routes import max_client_size_bytes, register

__all__ = ["register", "max_client_size_bytes"]
