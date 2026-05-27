"""Backplane log viewer (mirror of upstream ``/api/logs``).

Exposes a single route:

- ``GET /hermes/logs?file=agent|errors|gateway&lines=N&level=...&component=...&search=...``

Reads tail lines from ``$HERMES_HOME/logs/<name>.log`` and applies the
same level/component/search filters the upstream Status dashboard uses.
Pure read-only — no writes, no side effects.
"""

from __future__ import annotations

from .routes import register

__all__ = ["register"]
