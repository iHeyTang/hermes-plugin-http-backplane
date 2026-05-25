"""Read-only access to the Hermes curated memory files.

Hermes stores curated memory as two markdown files under
``$HERMES_HOME/memories``: ``MEMORY.md`` (agent self-notes) and ``USER.md``
(notes about the user). Entries within a file are separated by
``\\n§\\n``.

Defers the path + delimiter + content-scanner to upstream
``tools/memory_tool.py`` so this module never drifts from the agent's own
view of memory. The only thing we add here is structuring the data for
the HTTP surface and surfacing the upstream security scan's verdict on
each entry — Hermes uses the same regex set internally before injecting
memory into prompts (see ``_MEMORY_THREAT_PATTERNS`` in
``tools/memory_tool.py``), so anything it would flag is content the UI
should warn the user about before they trust it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ....adapters.hermes_core import hermes_home

logger = logging.getLogger("my-browser-bridge")

# Local fallbacks used only when upstream isn't importable in this
# process (e.g. the bridge running outside a Hermes install). The
# upstream-backed helpers below override these whenever possible so we
# never silently drift from `tools/memory_tool.py`.
_FALLBACK_ENTRY_DELIMITER = "\n§\n"
_FALLBACK_CHAR_LIMITS: Dict[str, int] = {
    "memory": 2200,
    "user": 1375,
}

MEMORY_TARGETS = ("memory", "user")

_FILE_NAMES: Dict[str, str] = {
    "memory": "MEMORY.md",
    "user": "USER.md",
}


def _entry_delimiter() -> str:
    try:
        from tools.memory_tool import ENTRY_DELIMITER  # type: ignore

        if isinstance(ENTRY_DELIMITER, str) and ENTRY_DELIMITER:
            return ENTRY_DELIMITER
    except Exception:
        pass
    return _FALLBACK_ENTRY_DELIMITER


def _char_limit(target: str) -> int:
    # No public accessor for MemoryStore limits in upstream — keep
    # mirroring the defaults locally; same numbers, single source of
    # truth via the comment cross-reference in the module docstring.
    return _FALLBACK_CHAR_LIMITS.get(target, 0)


def _memory_dir() -> Path:
    """Resolve the memories directory through upstream when available."""
    try:
        from tools.memory_tool import get_memory_dir  # type: ignore

        return Path(get_memory_dir())
    except Exception as exc:
        logger.debug("memory_tool.get_memory_dir unavailable: %s", exc)
        return hermes_home() / "memories"


def _path_for(target: str) -> Path:
    return _memory_dir() / _FILE_NAMES[target]


def _scan_entry(entry: str) -> Optional[str]:
    """Return the upstream threat-classification (e.g. ``"prompt_injection"``,
    ``"exfil_curl"``) when the entry trips Hermes's safety scanner,
    otherwise ``None``. The exact classification labels come from
    ``_MEMORY_THREAT_PATTERNS`` in ``tools/memory_tool.py``.
    """
    try:
        from tools.memory_tool import _scan_memory_content  # type: ignore

        verdict = _scan_memory_content(entry)
        if isinstance(verdict, str) and verdict:
            return verdict
        return None
    except Exception:
        return None


def _parse_entries(text: str) -> List[Dict[str, Any]]:
    """Split a memory file into structured entries with safety classification.

    Each entry surfaces as ``{"text", "flagged"}`` where ``flagged`` is
    ``null`` for clean entries and a short classification string for
    anything the upstream scanner would block from prompt-injection.
    """
    if not text:
        return []
    delim = _entry_delimiter()
    parts = text.split(delim)
    out: List[Dict[str, Any]] = []
    for raw in parts:
        body = raw.strip()
        if not body:
            continue
        out.append({"text": body, "flagged": _scan_entry(body)})
    return out


def read_memory_entries(target: str) -> Dict[str, Any]:
    if target not in MEMORY_TARGETS:
        raise ValueError(f"target must be one of: {', '.join(MEMORY_TARGETS)}")

    path = _path_for(target)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        entries: List[Dict[str, Any]] = []
    except OSError as exc:
        raise OSError(f"failed to read {path}: {exc}") from exc
    else:
        entries = _parse_entries(raw)

    delim = _entry_delimiter()
    char_count = (
        len(delim.join(e["text"] for e in entries)) if entries else 0
    )
    flagged_count = sum(1 for e in entries if e.get("flagged"))
    return {
        "target": target,
        "path": str(path),
        "entries": entries,
        "char_count": char_count,
        "char_limit": _char_limit(target),
        "flagged_count": flagged_count,
    }


def read_memory_entries_response(target: str) -> Dict[str, Any]:
    return {"ok": True, **read_memory_entries(target)}
