"""Hermes tools that let the agent manage ``/integrations/*`` endpoints.

Four tools, all sync (no real I/O beyond file writes and an immediate
register call):

- ``integration_list`` — what's registered right now, where it came from
- ``integration_install`` — write files under ``~/.hermes/integrations/<name>/``
  and hot-register; overwrite swaps routes atomically
- ``integration_remove`` — delete files AND drop the routes from the
  dispatcher's registry; the integration is fully gone on the next
  request
- ``integration_reload`` — re-import an existing integration from disk
  and atomically swap its router; picks up source edits without a
  restart

Every state-changing call appends one JSON line to
``~/.hermes/integrations/.audit.log`` so it's possible to reconstruct
who-touched-what after the fact.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.registry import tool_error, tool_result

from ...api import unregister_integration
from . import loader as _loader
from .loader import USER_INTEGRATIONS_DIR

logger = logging.getLogger(__name__)

# Same shape as ``register_integration`` — keep them lockstep so installing
# a name that the registry would later reject fails early.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_NAME_MAX_LEN = 32

_AUDIT_LOG = USER_INTEGRATIONS_DIR / ".audit.log"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_name(name: object) -> Optional[str]:
    if not isinstance(name, str) or not _NAME_RE.match(name) or len(name) > _NAME_MAX_LEN:
        return (
            f"invalid name {name!r}; must match {_NAME_RE.pattern} "
            f"(max {_NAME_MAX_LEN} chars)"
        )
    return None


def _resolve_target_dir(name: str) -> Path:
    """Return the canonical user-integration directory for *name*.

    The resolve+is_relative_to check is what stops a crafted name like
    ``../etc/passwd`` from escaping the user directory — even though the
    name regex would already block it, defense-in-depth here is cheap.
    """
    target = (USER_INTEGRATIONS_DIR / name).resolve()
    root = USER_INTEGRATIONS_DIR.resolve()
    if not (target == root or root in target.parents):
        raise ValueError(f"resolved path {target} escapes {root}")
    return target


def _audit(event: str, **fields: object) -> None:
    """Append one JSON line to the audit log. Best-effort — never raises."""
    try:
        USER_INTEGRATIONS_DIR.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.time(), "event": event, **fields}
        with _AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("audit log write failed (event=%s)", event)


def _list_current() -> List[Dict[str, object]]:
    """Snapshot the loader state in a JSON-shaped form."""
    out: List[Dict[str, object]] = []
    state = _loader.get_state()
    for entry in state.loaded:
        meta = entry.meta or {}
        out.append(
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
    return out


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


INTEGRATION_LIST_SCHEMA = {
    "name": "integration_list",
    "description": (
        "List all integrations currently registered under /integrations/<name>/* "
        "on the local HTTP backplane. Reports source (preset|user), filesystem "
        "path, declared endpoints (from integration.yaml), and any failed loads "
        "from the last scan."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


INTEGRATION_INSTALL_SCHEMA = {
    "name": "integration_install",
    "description": (
        "Write a new user integration to ~/.hermes/integrations/<name>/ and "
        "register it under /integrations/<name>/* without restarting Hermes. "
        "Routes go live on the next matching HTTP request; overwriting an "
        "existing name atomically swaps its router.\n\n"
        "Each integration is a tiny Python package: an __init__.py that "
        "exposes a setup(router) callable plus an integration.yaml with "
        "metadata. The simplest install passes 'handler_py' (the file that "
        "defines setup(router)) and lets __init__.py default to "
        "`from .handler import setup`.\n\n"
        "Pass 'from_path' to copy from an existing local directory instead "
        "of inlining content — useful for hand-crafted integrations.\n\n"
        "Refuses to overwrite an existing name unless 'overwrite' is true. "
        "Refuses names that collide with a built-in preset."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "URL-safe name; matches ^[a-z][a-z0-9-]*$.",
            },
            "handler_py": {
                "type": "string",
                "description": (
                    "Contents of handler.py. Must define `setup(router)` "
                    "or be re-exported from __init__.py."
                ),
            },
            "init_py": {
                "type": "string",
                "description": (
                    "Contents of __init__.py. Defaults to "
                    "`from .handler import setup` when 'handler_py' is given."
                ),
            },
            "yaml": {
                "type": "string",
                "description": (
                    "Contents of integration.yaml (metadata). Optional; "
                    "the loader tolerates a missing file."
                ),
            },
            "extra_files": {
                "type": "object",
                "description": (
                    "Additional files dropped alongside, keyed by relative "
                    "filename. Useful for splitting subprocess wrappers, "
                    "shared helpers, etc."
                ),
                "additionalProperties": {"type": "string"},
            },
            "from_path": {
                "type": "string",
                "description": (
                    "If set, copy the directory at this absolute path into "
                    "~/.hermes/integrations/<name>/ verbatim. When given, "
                    "the inline fields (handler_py/init_py/yaml/extra_files) "
                    "are ignored."
                ),
            },
            "overwrite": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Replace an existing integration with the same name. "
                    "Defaults to false (refuses on collision)."
                ),
            },
        },
        "required": ["name"],
    },
}


INTEGRATION_REMOVE_SCHEMA = {
    "name": "integration_remove",
    "description": (
        "Delete the files for a user integration at "
        "~/.hermes/integrations/<name>/ and drop its routes from the "
        "dispatcher's registry. The next request to /integrations/<name>/* "
        "returns 404; in-flight requests finish on the old handlers. "
        "Refuses to touch preset integrations."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Integration to remove."},
        },
        "required": ["name"],
    },
}


INTEGRATION_RELOAD_SCHEMA = {
    "name": "integration_reload",
    "description": (
        "Re-import ~/.hermes/integrations/<name>/ from disk and atomically "
        "swap the live router. Use this to pick up source edits made "
        "outside of integration_install. Works whether the integration "
        "was already registered or not — in both cases the dispatcher "
        "serves the freshly-imported routes on the next request."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Integration to reload."},
        },
        "required": ["name"],
    },
}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _handle_integration_list(args: dict, **_kw: Any) -> str:
    state = _loader.get_state()
    return tool_result(
        {
            "integrations": _list_current(),
            "failed": list(state.failed),
            "user_dir": str(USER_INTEGRATIONS_DIR),
        }
    )


def _handle_integration_install(args: dict, **_kw: Any) -> str:
    args = args or {}
    name = args.get("name")
    err = _validate_name(name)
    if err:
        return tool_error(f"integration_install: {err}")
    assert isinstance(name, str)

    # Reject collisions with presets up front: the loader would later
    # refuse anyway, but a clear error at install time beats finding out
    # via the loader log after the fact.
    if name in {n for n in _loader._discover_preset_names()}:
        return tool_error(
            f"integration_install: name {name!r} collides with a built-in preset"
        )

    overwrite = bool(args.get("overwrite", False))
    try:
        target = _resolve_target_dir(name)
    except ValueError as exc:
        return tool_error(f"integration_install: {exc}")

    if target.exists():
        if not overwrite:
            return tool_error(
                f"integration_install: {name!r} already exists at {target}; "
                "pass overwrite=true to replace it"
            )
        # Wipe in place. load_user_integration below will re-import the
        # fresh source and atomically swap the live router — in-flight
        # requests on the previous version finish on the old handlers.
        shutil.rmtree(target)

    target.mkdir(parents=True, exist_ok=False)

    # --- source: copy from path -------------------------------------------
    from_path = args.get("from_path")
    if isinstance(from_path, str) and from_path:
        src = Path(from_path).expanduser()
        if not src.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            return tool_error(
                f"integration_install: from_path {src} is not a directory"
            )
        # Re-create target by copying (copytree refuses if target exists).
        shutil.rmtree(target)
        shutil.copytree(src, target)
    else:
        # --- source: inline strings ---------------------------------------
        handler_py = args.get("handler_py")
        init_py = args.get("init_py")
        yaml_content = args.get("yaml")
        extra_files = args.get("extra_files") or {}

        if not isinstance(handler_py, str) and not isinstance(init_py, str):
            shutil.rmtree(target, ignore_errors=True)
            return tool_error(
                "integration_install: provide 'handler_py' (or 'init_py') "
                "or a 'from_path' source"
            )

        if isinstance(handler_py, str):
            (target / "handler.py").write_text(handler_py, encoding="utf-8")

        # Default __init__.py: re-export setup from handler.py.
        if not isinstance(init_py, str):
            init_py = "from .handler import setup  # noqa: F401\n"
        (target / "__init__.py").write_text(init_py, encoding="utf-8")

        if isinstance(yaml_content, str) and yaml_content.strip():
            (target / "integration.yaml").write_text(yaml_content, encoding="utf-8")

        if isinstance(extra_files, dict):
            for filename, content in extra_files.items():
                if not isinstance(filename, str) or not isinstance(content, str):
                    continue
                # Defense-in-depth: refuse path separators in extra filenames
                # so the agent can't escape the integration dir.
                if "/" in filename or "\\" in filename or filename.startswith("."):
                    continue
                (target / filename).write_text(content, encoding="utf-8")

    # --- hot mount ---------------------------------------------------------
    try:
        entry = _loader.load_user_integration(name)
    except Exception as exc:
        logger.exception("integration_install: load failed for %s", name)
        _audit("install_failed", name=name, error=str(exc))
        return tool_error(
            f"integration_install: files written to {target}, but loading "
            f"failed: {exc}. Fix the integration on disk and call "
            "integration_reload."
        )

    _audit("install", name=name, path=str(target), overwrite=overwrite)
    return tool_result(
        {
            "ok": True,
            "name": name,
            "path": str(entry.path),
            "mount": f"/integrations/{name}/",
            "meta": entry.meta,
            "note": (
                "Registered live; HTTP requests to /integrations/"
                f"{name}/* are served on the next request."
            ),
        }
    )


def _handle_integration_remove(args: dict, **_kw: Any) -> str:
    args = args or {}
    name = args.get("name")
    err = _validate_name(name)
    if err:
        return tool_error(f"integration_remove: {err}")
    assert isinstance(name, str)

    if name in {n for n in _loader._discover_preset_names()}:
        return tool_error(
            f"integration_remove: {name!r} is a preset; remove it from the "
            "backplane package instead"
        )

    try:
        target = _resolve_target_dir(name)
    except ValueError as exc:
        return tool_error(f"integration_remove: {exc}")

    if not target.exists():
        return tool_error(f"integration_remove: {name!r} not found at {target}")

    shutil.rmtree(target)

    # Drop the routes from the runtime registry: the next request to
    # /integrations/<name>/* gets a 404 from the dispatcher. In-flight
    # requests finish on the old handlers since the dispatcher snapshots
    # the route list per request.
    unregistered = unregister_integration(name)

    # Keep the loader snapshot in sync.
    state = _loader.get_state()
    state.loaded[:] = [e for e in state.loaded if e.name != name]

    _audit("remove", name=name, path=str(target), unregistered=unregistered)
    return tool_result(
        {
            "ok": True,
            "name": name,
            "deleted_path": str(target),
            "unregistered": unregistered,
            "note": (
                f"Files removed and routes dropped. /integrations/{name}/* "
                "returns 404 on the next request."
            ),
        }
    )


def _handle_integration_reload(args: dict, **_kw: Any) -> str:
    args = args or {}
    name = args.get("name")
    err = _validate_name(name)
    if err:
        return tool_error(f"integration_reload: {err}")
    assert isinstance(name, str)

    try:
        target = _resolve_target_dir(name)
    except ValueError as exc:
        return tool_error(f"integration_reload: {exc}")

    if not target.exists() or not (target / "__init__.py").exists():
        return tool_error(
            f"integration_reload: {name!r} not found at {target}"
        )

    # ``load_user_integration`` clears the package out of ``sys.modules``
    # before re-importing (see loader._load_user), so this picks up
    # source edits made outside of integration_install. The replace=True
    # path inside register_integration atomically swaps the router.
    try:
        entry = _loader.load_user_integration(name)
    except Exception as exc:
        logger.exception("integration_reload: load failed for %s", name)
        _audit("reload_failed", name=name, error=str(exc))
        return tool_error(f"integration_reload: load failed: {exc}")

    _audit("reload", name=name, path=str(target))
    return tool_result(
        {
            "ok": True,
            "name": name,
            "path": str(entry.path),
            "mount": f"/integrations/{name}/",
            "meta": entry.meta,
            "note": (
                "Re-imported from disk and router swapped atomically; "
                f"/integrations/{name}/* now serves the latest source."
            ),
        }
    )


# ---------------------------------------------------------------------------
# Public registration bundle
# ---------------------------------------------------------------------------


# Tuples consumed by the plugin's register(ctx) entry point. Shape matches
# what the browser-tools plugin uses: (name, schema, handler, emoji).
TOOLS = (
    ("integration_list", INTEGRATION_LIST_SCHEMA, _handle_integration_list, "🧩"),
    ("integration_install", INTEGRATION_INSTALL_SCHEMA, _handle_integration_install, "📥"),
    ("integration_remove", INTEGRATION_REMOVE_SCHEMA, _handle_integration_remove, "🗑"),
    ("integration_reload", INTEGRATION_RELOAD_SCHEMA, _handle_integration_reload, "🔄"),
)
