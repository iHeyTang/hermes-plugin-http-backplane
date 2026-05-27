"""
``~/.hermes/.env`` — the single source of truth shared with the Hermes
runtime. Read, merge, and apply to ``os.environ``.

Historical note: an earlier version of this module wrote credentials
into ``<plugin-root>/.env``, isolated from Hermes. That caused
"provider configured in extension but ``authenticated: false`` in
Hermes" mismatches because Hermes only reads its own ``~/.hermes/.env``.
We now point at the Hermes file directly. ``_migrate_legacy_plugin_env``
runs once on startup to move any keys still living in the old location
so existing installs don't lose their saved credentials.

Bridge startup uses ``setdefault`` only. User edits from the extension use
``merge_dotenv_file_and_apply`` so the running process sees new keys immediately.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger("my-browser-bridge")

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent

_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _hermes_home() -> Path:
    """Same resolution Hermes uses: env override → ``~/.hermes``."""
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def plugin_dotenv_path(base: Path | None = None) -> Path:
    """Path to the dotenv file. ``base`` is accepted for test injection;
    when omitted, returns Hermes' own ``~/.hermes/.env`` so saved
    credentials are visible to the Hermes runtime process.
    """
    if base is not None:
        return base / ".env"
    home = _hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    return home / ".env"


def _legacy_plugin_dotenv_path() -> Path:
    """Pre-migration location: ``<plugin-root>/.env``. Read-only at this
    point; ``_migrate_legacy_plugin_env`` drains it into the canonical
    file and removes it.
    """
    return _PLUGIN_ROOT / ".env"


def is_valid_env_key(name: str) -> bool:
    return bool(name and _ENV_KEY_RE.match(name))


def read_dotenv_as_dict(path: Path) -> Dict[str, str]:
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return {}
    out: Dict[str, str] = {}
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        key, _, val = s.partition("=")
        key = key.strip()
        if not key or not is_valid_env_key(key):
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


def _fmt_dotenv_value(val: str) -> str:
    if not val:
        return '""'
    if any(c in val for c in ' "\n\\#'):
        esc = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{esc}"'
    return val


def merge_dotenv_file_and_apply(
    updates: Dict[str, str],
    *,
    base: Path | None = None,
) -> Dict[str, str]:
    """Merge *updates* into ``.env`` and set ``os.environ`` (empty value deletes)."""
    path = plugin_dotenv_path(base)
    cur = read_dotenv_as_dict(path)
    for k, v in updates.items():
        if not is_valid_env_key(k):
            continue
        if v == "":
            cur.pop(k, None)
            os.environ.pop(k, None)
        else:
            cur[k] = v
            os.environ[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"{k}={_fmt_dotenv_value(v)}" for k, v in sorted(cur.items()))
    path.write_text(body + ("\n" if body else ""), encoding="utf-8")
    logger.info("Updated %s (%d keys)", path, len(cur))
    return cur


def get_dotenv_values_for_keys(keys: List[str], *, base: Path | None = None) -> Dict[str, str]:
    """Values for UI: prefer on-disk ``.env``, else ``os.environ``."""
    path = plugin_dotenv_path(base)
    file_vals = read_dotenv_as_dict(path)
    out: Dict[str, str] = {}
    for k in keys:
        if k in file_vals:
            out[k] = file_vals[k]
        else:
            out[k] = os.environ.get(k, "") or ""
    return out


def _migrate_legacy_plugin_env() -> None:
    """One-shot: drain ``<plugin-root>/.env`` into ``~/.hermes/.env``.

    Keys already set in the canonical file win — we only fill blanks.
    The legacy file is removed after a successful merge so we don't
    diverge again on the next save. Silently no-ops when the legacy
    file is absent (clean install or already migrated).
    """
    legacy = _legacy_plugin_dotenv_path()
    if not legacy.is_file():
        return
    canonical = plugin_dotenv_path()
    try:
        legacy_vals = read_dotenv_as_dict(legacy)
    except Exception:
        return
    if not legacy_vals:
        try:
            legacy.unlink()
        except OSError:
            pass
        return
    canonical_vals = read_dotenv_as_dict(canonical) if canonical.is_file() else {}
    additions: Dict[str, str] = {}
    for k, v in legacy_vals.items():
        if not is_valid_env_key(k):
            continue
        if (canonical_vals.get(k) or "").strip():
            continue
        if not (v or "").strip():
            continue
        additions[k] = v
    if additions:
        try:
            merge_dotenv_file_and_apply(additions, base=canonical.parent)
        except Exception as exc:
            logger.warning("Failed to merge legacy plugin .env: %s", exc)
            return
    try:
        legacy.unlink()
        logger.info(
            "Migrated %d key(s) from legacy %s → %s and removed legacy file",
            len(additions),
            legacy,
            canonical,
        )
    except OSError as exc:
        logger.warning("Could not remove legacy %s: %s", legacy, exc)


def apply_plugin_dotenv(base: Path | None = None) -> None:
    """Load the canonical dotenv into ``os.environ`` (only where unset).

    Also runs one-shot migration from the legacy ``<plugin-root>/.env``
    before reading, so the very first startup after this change picks
    up keys the user previously saved through the extension.
    """
    if base is None:
        _migrate_legacy_plugin_env()
    path = plugin_dotenv_path(base)
    if not path.is_file():
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return
    n = 0
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        key, _, val = s.partition("=")
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key not in os.environ:
            os.environ[key] = val
            n += 1
    if n:
        logger.info("Applied %d key(s) from %s (only where unset)", n, path)

