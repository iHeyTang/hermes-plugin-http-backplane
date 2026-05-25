"""Integration feature lane.

The backplane owns ``/integrations/<name>/*`` end-to-end. Integrations are
loaded from two roots, both flowing through the same ``register_integration``
queue in ``runtime.api``:

- **Presets** — shipped inside this package under ``presets/<name>/``.
- **User integrations** — writable, at ``~/.hermes/integrations/<name>/``.

Each integration directory is a small Python package: an ``integration.yaml``
metadata file plus an ``__init__.py`` (or ``handler.py`` it re-exports from)
that exposes ``setup(router) -> None``.

The public surface for callers in this package is:

- :func:`load_all` — discover everything, register with the queue. Called
  once during HTTP app boot, before :func:`integrations_mount.mount_all`.
- :data:`USER_INTEGRATIONS_DIR` — the writable directory, created on demand
  by the install tool.
"""

from __future__ import annotations

from .loader import USER_INTEGRATIONS_DIR, load_all  # noqa: F401
