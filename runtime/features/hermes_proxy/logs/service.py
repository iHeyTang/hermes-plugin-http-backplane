"""Tail + filter logic for ``GET /hermes/logs``.

Delegates to upstream's own ``hermes_cli.logs._read_tail`` so filtering
behavior (level threshold, component prefix matching, large-file chunked
reads) stays aligned with the dashboard. Defensive imports — when the
upstream module isn't importable in this install we degrade to an empty
``LOG_FILES`` map and the route surfaces a 400.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ....adapters.hermes_core import hermes_home

logger = logging.getLogger(__name__)


def _load_upstream() -> Tuple[Dict[str, str], Dict[str, Sequence[str]], Any]:
    """Return ``(LOG_FILES, COMPONENT_PREFIXES, _read_tail)``.

    Each piece is best-effort: an unimportable upstream just yields empty
    maps and a no-op tailer, so the route can still 400 cleanly instead
    of 500.
    """
    try:
        from hermes_cli.logs import LOG_FILES, _read_tail  # type: ignore
    except Exception:
        LOG_FILES = {}
        def _read_tail(*_a, **_kw):  # type: ignore[no-redef]
            return []
    try:
        from hermes_logging import COMPONENT_PREFIXES  # type: ignore
    except Exception:
        COMPONENT_PREFIXES = {}
    return LOG_FILES, COMPONENT_PREFIXES, _read_tail


def available_log_files() -> List[str]:
    """Return the file-key whitelist (e.g. ``["agent", "errors", "gateway"]``)."""
    LOG_FILES, _, _ = _load_upstream()
    return sorted(LOG_FILES.keys())


def available_components() -> List[str]:
    LOG_FILES, COMPONENT_PREFIXES, _ = _load_upstream()
    return sorted(COMPONENT_PREFIXES.keys())


class LogsError(Exception):
    """Raised when the caller asks for an unknown file or component."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def read_logs(
    *,
    file: str = "agent",
    lines: int = 100,
    level: Optional[str] = None,
    component: Optional[str] = None,
    search: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the ``GET /hermes/logs`` payload.

    Shape matches upstream ``/api/logs``: ``{"file": str, "lines": [str]}``.
    Raises :class:`LogsError` for whitelist violations so the route can
    map them to 400.
    """
    LOG_FILES, COMPONENT_PREFIXES, _read_tail = _load_upstream()

    log_name = LOG_FILES.get(file)
    if not log_name:
        raise LogsError(400, f"Unknown log file: {file}")
    log_path: Path = hermes_home() / "logs" / log_name
    if not log_path.exists():
        return {"file": file, "lines": []}

    min_level = level if level and level.upper() != "ALL" else None
    comp_prefixes: Optional[Sequence[str]]
    if component and component.lower() != "all":
        comp_prefixes = COMPONENT_PREFIXES.get(component)
        if comp_prefixes is None:
            avail = ", ".join(sorted(COMPONENT_PREFIXES))
            raise LogsError(
                400, f"Unknown component: {component}. Available: {avail}"
            )
    else:
        comp_prefixes = None

    capped = max(1, min(int(lines or 100), 500))
    has_filters = bool(min_level or comp_prefixes or search)
    raw_lines = _read_tail(
        log_path,
        capped if not search else 2000,
        has_filters=has_filters,
        min_level=min_level,
        component_prefixes=comp_prefixes,
    )

    if search:
        needle = search.lower()
        raw_lines = [l for l in raw_lines if needle in l.lower()][-capped:]

    return {"file": file, "lines": raw_lines}
