"""Integration lifecycle operations as plain Python.

Pure manager layer: list / install / remove / reload, returning plain
dicts and raising plain exceptions. Two callers wrap this:

- :mod:`hermes_plugin_http_backplane.cli` — operator-facing CLI
- :mod:`runtime.features.hermes_proxy.integrations_admin.routes` —
  HTTP admin endpoints that the CLI talks to when the backplane is up

Deliberately knows nothing about the Hermes agent's tool-registry layer.
Integration management is a configuration concern, not an agent capability.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...api import unregister_integration
from . import loader as _loader
from .loader import USER_INTEGRATIONS_DIR

logger = logging.getLogger(__name__)

# Same shape as register_integration's validation — keep lockstep so an
# install that would later be rejected by the registry fails up front.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_NAME_MAX_LEN = 32

_AUDIT_LOG = USER_INTEGRATIONS_DIR / ".audit.log"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class IntegrationError(Exception):
    """Raised by manager operations. CLI prints .args[0]; HTTP turns it into 400."""


class NameInvalid(IntegrationError):
    pass


class NameTaken(IntegrationError):
    """Refused to overwrite an existing user integration."""


class NameReserved(IntegrationError):
    """Name collides with a built-in preset."""


class NotFound(IntegrationError):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def validate_name(name: object) -> str:
    """Raise NameInvalid for anything that wouldn't pass the registry."""
    if (
        not isinstance(name, str)
        or not _NAME_RE.match(name)
        or len(name) > _NAME_MAX_LEN
    ):
        raise NameInvalid(
            f"invalid name {name!r}; must match {_NAME_RE.pattern} "
            f"(max {_NAME_MAX_LEN} chars)"
        )
    return name


def _resolve_target_dir(name: str) -> Path:
    """Canonical ``~/.hermes/integrations/<name>/`` for *name*.

    Resolve + ancestry check belt-and-braces the regex above so a crafted
    name (``../etc/passwd``) can't escape the user directory even if a
    future tweak to the regex slips through.
    """
    target = (USER_INTEGRATIONS_DIR / name).resolve()
    root = USER_INTEGRATIONS_DIR.resolve()
    if not (target == root or root in target.parents):
        raise NameInvalid(f"resolved path {target} escapes {root}")
    return target


def _audit(event: str, **fields: object) -> None:
    """Append one JSON line to the audit log. Best-effort — never raises.

    The log is the only after-the-fact way to reconstruct who-installed-
    what when something behaves unexpectedly. Cheap insurance.
    """
    try:
        USER_INTEGRATIONS_DIR.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.time(), "event": event, **fields}
        with _AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("audit log write failed (event=%s)", event)


def _preset_names() -> set[str]:
    return set(_loader._discover_preset_names())


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def list_integrations() -> Dict[str, Any]:
    """Snapshot what's currently loaded + the last scan's failures."""
    state = _loader.get_state()
    loaded: List[Dict[str, Any]] = []
    for entry in state.loaded:
        meta = entry.meta or {}
        loaded.append(
            {
                "name": entry.name,
                "source": entry.source,
                "path": str(entry.path),
                "mount": f"/integrations/{entry.name}/",
                "version": meta.get("version"),
                "description": meta.get("description"),
                "endpoints": meta.get("endpoints"),
            }
        )
    return {
        "integrations": loaded,
        "failed": list(state.failed),
        "user_dir": str(USER_INTEGRATIONS_DIR),
    }


def install(
    name: str,
    *,
    handler_py: Optional[str] = None,
    init_py: Optional[str] = None,
    yaml: Optional[str] = None,
    extra_files: Optional[Dict[str, str]] = None,
    from_path: Optional[str] = None,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Write files for *name* under ``~/.hermes/integrations/<name>/``.

    Does **not** trigger a live re-register itself — that's the caller's
    job. The CLI POSTs ``/hermes/integrations/reload`` after this; the
    HTTP admin endpoint chains :func:`reload` directly.

    Sources:
    - ``from_path``: copy a local directory verbatim (handler_py/init_py/
      yaml/extra_files are ignored when set)
    - inline: provide ``handler_py`` (and optionally ``init_py``, ``yaml``,
      ``extra_files``); a default ``__init__.py`` re-exports ``setup`` from
      ``handler.py`` if ``init_py`` is not given
    """
    validate_name(name)

    if name in _preset_names():
        raise NameReserved(f"name {name!r} collides with a built-in preset")

    target = _resolve_target_dir(name)
    if target.exists():
        if not overwrite:
            raise NameTaken(
                f"{name!r} already exists at {target}; pass overwrite=True "
                "to replace"
            )
        shutil.rmtree(target)

    target.mkdir(parents=True, exist_ok=False)

    try:
        if isinstance(from_path, str) and from_path:
            src = Path(from_path).expanduser()
            if not src.is_dir():
                raise IntegrationError(f"from_path {src} is not a directory")
            # copytree refuses to overwrite, so wipe and re-create.
            shutil.rmtree(target)
            shutil.copytree(src, target)
        else:
            if handler_py is None and init_py is None:
                raise IntegrationError(
                    "provide handler_py (or init_py) or a from_path source"
                )
            if isinstance(handler_py, str):
                (target / "handler.py").write_text(handler_py, encoding="utf-8")
            if init_py is None:
                init_py = "from .handler import setup  # noqa: F401\n"
            (target / "__init__.py").write_text(init_py, encoding="utf-8")
            if isinstance(yaml, str) and yaml.strip():
                (target / "integration.yaml").write_text(yaml, encoding="utf-8")
            if extra_files:
                for filename, content in extra_files.items():
                    if not isinstance(filename, str) or not isinstance(content, str):
                        continue
                    # Defense-in-depth: refuse separators / dotfiles so a
                    # malformed manifest can't escape the integration dir.
                    if "/" in filename or "\\" in filename or filename.startswith("."):
                        continue
                    (target / filename).write_text(content, encoding="utf-8")
    except Exception:
        # Roll back the directory we just created so a half-written
        # install doesn't poison a later retry.
        shutil.rmtree(target, ignore_errors=True)
        raise

    _audit("install", name=name, path=str(target), overwrite=overwrite)
    return {
        "ok": True,
        "name": name,
        "path": str(target),
        "wrote": sorted(p.name for p in target.iterdir()),
    }


def remove(name: str) -> Dict[str, Any]:
    """Delete files + drop routes from the live registry."""
    validate_name(name)

    if name in _preset_names():
        raise NameReserved(
            f"{name!r} is a preset; remove it from the backplane package instead"
        )

    target = _resolve_target_dir(name)
    if not target.exists():
        raise NotFound(f"{name!r} not found at {target}")

    shutil.rmtree(target)
    unregistered = unregister_integration(name)

    state = _loader.get_state()
    state.loaded[:] = [e for e in state.loaded if e.name != name]

    _audit("remove", name=name, path=str(target), unregistered=unregistered)
    return {
        "ok": True,
        "name": name,
        "deleted_path": str(target),
        "unregistered": unregistered,
    }


def reload(name: str) -> Dict[str, Any]:
    """Re-import + atomically swap the router for an existing integration."""
    validate_name(name)

    target = _resolve_target_dir(name)
    if not target.exists() or not (target / "__init__.py").exists():
        raise NotFound(f"{name!r} not found at {target}")

    try:
        entry = _loader.load_user_integration(name)
    except Exception as exc:
        _audit("reload_failed", name=name, error=str(exc))
        raise IntegrationError(f"reload failed: {exc}") from exc

    _audit("reload", name=name, path=str(target))
    return {
        "ok": True,
        "name": name,
        "path": str(entry.path),
        "mount": f"/integrations/{name}/",
        "meta": entry.meta,
    }
