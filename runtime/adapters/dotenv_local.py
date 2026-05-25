"""
Optional ``<plugin-root>/.env`` — load, read, merge, and apply to ``os.environ``.

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


def plugin_dotenv_path(base: Path | None = None) -> Path:
    root = base if base is not None else _PLUGIN_ROOT
    return root / ".env"


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


def apply_plugin_dotenv(base: Path | None = None) -> None:
    root = base if base is not None else _PLUGIN_ROOT
    path = root / ".env"
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

