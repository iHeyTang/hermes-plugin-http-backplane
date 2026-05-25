"""Integration discovery and load.

Two sources, one queue:

1. **Presets** — sub-packages of ``runtime.features.integrations.presets``.
   Imported via :func:`importlib.import_module` like normal Python modules.
2. **User integrations** — directories under ``~/.hermes/integrations/``.
   Each directory is imported as a free-standing package named
   ``hermes_integration_<name>`` via :func:`importlib.util.spec_from_file_location`
   with ``submodule_search_locations`` set, so relative imports inside
   the integration (e.g. ``from .lark_cli import ...``) resolve cleanly.

For both sources, after import we expect a ``setup(router)`` attribute and
hand it to :func:`runtime.api.register_integration`. Errors in one
integration are logged and skipped — they must not break the boot of the
others or of the backplane itself.

The user directory is created lazily by ``integration_install``; this
loader is fine with it not existing.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import pkgutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ...api import register_integration

logger = logging.getLogger(__name__)

# Type alias kept loose; the real shape is ``Callable[[aiohttp.web.UrlDispatcher], None]``.
SetupFn = Callable[[object], None]

# Resolve HERMES_HOME like the rest of Hermes: env override > ~/.hermes.
# We don't import hermes_constants here because this module loads early
# (during HTTP app build) and a hard dependency on core helpers would
# couple boot order needlessly. The env var is the authoritative knob
# anyway; the ``~/.hermes`` fallback matches hermes_constants's default.
_HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
USER_INTEGRATIONS_DIR = _HERMES_HOME / "integrations"

_PRESETS_PACKAGE = "hermes_plugin_http_backplane.runtime.features.integrations.presets"


@dataclass
class LoadedIntegration:
    """An integration that successfully imported and registered.

    Used by the ``integration_list`` tool to show provenance to the agent.
    Metadata (``meta``) is whatever ``integration.yaml`` contained, with the
    on-disk ``name`` and resolved ``path`` always present.
    """

    name: str
    source: str  # "preset" | "user"
    path: Path
    meta: Dict[str, object] = field(default_factory=dict)


@dataclass
class LoadResult:
    loaded: List[LoadedIntegration] = field(default_factory=list)
    failed: List[Dict[str, str]] = field(default_factory=list)


# Module-level state so ``integration_list`` / ``integration_install`` can
# query what's currently known. The HTTP server thread is the only writer
# from ``load_all`` (boot) and from tool calls (post-boot, same thread as
# the agent's tool dispatcher). Reads are best-effort snapshots.
_state: LoadResult = LoadResult()


def get_state() -> LoadResult:
    """Return the snapshot of loaded + failed integrations."""
    return _state


def _read_meta(integration_dir: Path) -> Dict[str, object]:
    """Parse ``integration.yaml`` if present. Tolerant of missing/empty files.

    YAML parsing failures are logged and treated as "no metadata" — the
    integration still loads as long as ``setup`` is importable. This
    keeps the loader from getting stuck on a typo in a comment.
    """
    yaml_path = integration_dir / "integration.yaml"
    if not yaml_path.exists():
        return {}
    try:
        import yaml  # local import — pyyaml is a hermes-agent dep
    except ImportError:
        logger.warning("pyyaml not available; skipping %s metadata", yaml_path)
        return {}
    try:
        with yaml_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("failed to parse %s: %s", yaml_path, exc)
        return {}


def _load_preset(name: str) -> Optional[SetupFn]:
    """Import a preset sub-package and pull its ``setup`` callable."""
    module_name = f"{_PRESETS_PACKAGE}.{name}"
    mod = importlib.import_module(module_name)
    setup = getattr(mod, "setup", None)
    if not callable(setup):
        raise AttributeError(
            f"preset {name!r} ({module_name}) has no callable 'setup'"
        )
    return setup


def _load_user(integration_dir: Path) -> SetupFn:
    """Import a user integration directory as a synthetic package.

    The package name is namespaced (``hermes_integration_<name>``) so that
    user integrations can't collide with backplane modules or preset names
    in ``sys.modules``. ``submodule_search_locations`` makes Python treat
    the directory as a package, so ``from .lark_cli import ...`` inside
    ``__init__.py`` / ``handler.py`` resolves to siblings in the same
    directory.
    """
    name = integration_dir.name
    pkg_name = f"hermes_integration_{name}"
    init_path = integration_dir / "__init__.py"
    if not init_path.exists():
        raise FileNotFoundError(
            f"user integration {name!r} missing __init__.py at {init_path}"
        )

    # Drop any previous import so reload-ish behavior is consistent (and so
    # a half-loaded module from a prior failed attempt doesn't poison this
    # one). Submodules under the same prefix get cleared too.
    for stale in [k for k in sys.modules if k == pkg_name or k.startswith(pkg_name + ".")]:
        del sys.modules[stale]

    spec = importlib.util.spec_from_file_location(
        pkg_name,
        init_path,
        submodule_search_locations=[str(integration_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build import spec for {init_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = mod
    spec.loader.exec_module(mod)

    setup = getattr(mod, "setup", None)
    if not callable(setup):
        raise AttributeError(f"user integration {name!r} has no callable 'setup'")
    return setup


def _discover_preset_names() -> List[str]:
    """Sub-packages of ``presets`` (one directory each, with ``__init__.py``)."""
    try:
        presets_pkg = importlib.import_module(_PRESETS_PACKAGE)
    except ImportError:
        return []
    names: List[str] = []
    for info in pkgutil.iter_modules(presets_pkg.__path__):
        if info.ispkg:
            names.append(info.name)
    return sorted(names)


def _discover_user_dirs() -> List[Path]:
    """User integration directories at ``~/.hermes/integrations/<name>/``.

    Skips dotfiles (e.g. ``.audit.log``), regular files, and anything
    without an ``__init__.py`` — the latter rules out half-finished
    installs and accidental ``mkdir``s.
    """
    if not USER_INTEGRATIONS_DIR.exists():
        return []
    out: List[Path] = []
    for entry in sorted(USER_INTEGRATIONS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not (entry / "__init__.py").exists():
            logger.info(
                "skipping %s: no __init__.py (not a complete integration)",
                entry,
            )
            continue
        out.append(entry)
    return out


def load_all() -> LoadResult:
    """Discover presets + user integrations and register each.

    Idempotent on re-entry by name (the underlying ``register_integration``
    already de-dupes), so safe to call again from a tool handler that
    just installed a new user integration.
    """
    global _state
    result = LoadResult()

    # Presets first so user integrations can override a same-named preset
    # cleanly — wait, can they? ``register_integration`` rejects duplicate
    # names today. We honor that: presets win, user dirs with the same
    # name are skipped with a clear log line. Override semantics can come
    # later if there's real demand.
    seen: set[str] = set()

    for name in _discover_preset_names():
        path = Path(_resolve_preset_path(name))
        try:
            setup = _load_preset(name)
        except Exception as exc:
            logger.exception("preset %s failed to import", name)
            result.failed.append({"name": name, "source": "preset", "error": str(exc)})
            continue
        meta = _read_meta(path)
        try:
            register_integration(name, setup)
        except Exception as exc:
            logger.exception("preset %s register_integration failed", name)
            result.failed.append({"name": name, "source": "preset", "error": str(exc)})
            continue
        result.loaded.append(
            LoadedIntegration(name=name, source="preset", path=path, meta=meta)
        )
        seen.add(name)

    for integration_dir in _discover_user_dirs():
        name = integration_dir.name
        if name in seen:
            logger.warning(
                "user integration %s shadows a preset of the same name; skipping",
                name,
            )
            result.failed.append(
                {
                    "name": name,
                    "source": "user",
                    "error": "name collides with a preset",
                }
            )
            continue
        try:
            setup = _load_user(integration_dir)
        except Exception as exc:
            logger.exception("user integration %s failed to import", name)
            result.failed.append({"name": name, "source": "user", "error": str(exc)})
            continue
        meta = _read_meta(integration_dir)
        try:
            register_integration(name, setup)
        except Exception as exc:
            logger.exception("user integration %s register_integration failed", name)
            result.failed.append({"name": name, "source": "user", "error": str(exc)})
            continue
        result.loaded.append(
            LoadedIntegration(
                name=name, source="user", path=integration_dir, meta=meta
            )
        )
        seen.add(name)

    _state = result
    logger.info(
        "integrations loaded: %d ok, %d failed",
        len(result.loaded), len(result.failed),
    )
    return result


def _resolve_preset_path(name: str) -> str:
    """Filesystem path to a preset directory (best-effort, for display)."""
    try:
        mod = importlib.import_module(f"{_PRESETS_PACKAGE}.{name}")
        return str(Path(mod.__file__).parent)  # type: ignore[arg-type]
    except Exception:
        return f"<preset:{name}>"


def load_user_integration(name: str) -> LoadedIntegration:
    """Load a single user integration (used by ``integration_install`` /
    ``integration_reload``).

    Returns the LoadedIntegration on success, or raises. Passes
    ``replace=True`` to :func:`register_integration` so re-installing or
    reloading an existing name atomically swaps the live router for the
    freshly-imported one. Updates the global state so ``integration_list``
    sees the new entry (or refreshed entry) without a full re-scan.
    """
    integration_dir = USER_INTEGRATIONS_DIR / name
    setup = _load_user(integration_dir)
    meta = _read_meta(integration_dir)
    register_integration(name, setup, replace=True)
    entry = LoadedIntegration(
        name=name, source="user", path=integration_dir, meta=meta
    )
    # Replace any existing entry for this name in the snapshot so the
    # loader's view stays in sync with the registry.
    _state.loaded[:] = [e for e in _state.loaded if e.name != name]
    _state.loaded.append(entry)
    return entry
