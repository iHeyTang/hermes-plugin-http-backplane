"""``hermes integration`` — operator-facing subcommand for backplane integrations.

Wired into Hermes's umbrella CLI via ``ctx.register_cli_command(...)`` at
plugin load time; see :func:`hermes_plugin_http_backplane.register`. The
two exports the plugin glue cares about are:

- :func:`register_subparser` — Hermes hands us an ``argparse`` subparser
  for the ``integration`` command; this function populates it with
  ``list / install / remove / reload`` sub-subparsers and wires each
  one's ``func`` to a dispatcher below.
- :func:`run` — fallback handler when the user types ``hermes integration``
  with no subcommand. Just prints help.

Designed so the same process the CLI runs in does NOT need the backplane
HTTP server up:

- ``list``, ``install``, ``remove`` fall back to the in-process manager
  when the backplane isn't reachable (file work still happens; live
  swap is skipped with a printed note)
- ``reload`` needs the backplane (it re-imports + swaps the live router),
  so it errors out cleanly if the port is dead
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib import error as urlerror
from urllib import request as urlrequest

DEFAULT_PORT = 9394
DEFAULT_HOST = "127.0.0.1"


# ---------------------------------------------------------------------------
# Backplane HTTP client (best-effort)
# ---------------------------------------------------------------------------


def _port() -> int:
    return int(os.environ.get("HERMES_BACKPLANE_PORT") or DEFAULT_PORT)


def _url(path: str) -> str:
    return f"http://{DEFAULT_HOST}:{_port()}{path}"


def _http(
    method: str, path: str, *, timeout: float = 2.0
) -> Tuple[Optional[int], Optional[Dict[str, Any]], Optional[str]]:
    """Call the backplane. Returns (status, body_json, error_message).

    ``status`` is None when the backplane isn't reachable (the caller
    decides whether that's fine or fatal). When the server returned a
    non-2xx with a JSON body, ``error_message`` is set from the body's
    ``error`` field so callers can surface a real message instead of
    "HTTP 404".
    """
    req = urlrequest.Request(_url(path), method=method)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body) if body.strip() else {}
            return resp.status, data, None
    except urlerror.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8"))
        except Exception:
            data = None
        msg = (data or {}).get("error") if isinstance(data, dict) else None
        return exc.code, data if isinstance(data, dict) else None, msg or exc.reason
    except urlerror.URLError:
        return None, None, None


def _backplane_alive() -> bool:
    status, _, _ = _http("GET", "/hermes/integrations")
    return status is not None


# ---------------------------------------------------------------------------
# Pretty output
# ---------------------------------------------------------------------------


def _as_json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _emit(data: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if isinstance(data, str):
        print(data)
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _emit_list(payload: Dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        _emit(payload, as_json=True)
        return
    integrations = payload.get("integrations") or []
    failed = payload.get("failed") or []
    user_dir = payload.get("user_dir") or ""
    if not integrations and not failed:
        print(f"(no integrations registered; user dir: {user_dir})")
        return
    for entry in integrations:
        mount = entry.get("mount") or ""
        name = entry.get("name") or ""
        source = entry.get("source") or ""
        version = entry.get("version") or ""
        desc = entry.get("description") or ""
        suffix = f"  {desc}" if desc else ""
        print(f"{mount:<32} {source:<8} {version}{suffix}".rstrip())
    for entry in failed:
        name = entry.get("name") or ""
        source = entry.get("source") or ""
        err = entry.get("error") or ""
        print(f"  ! {name} ({source}): {err}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Subcommand dispatchers
# ---------------------------------------------------------------------------


def _cmd_list(args: argparse.Namespace) -> int:
    status, data, err = _http("GET", "/hermes/integrations")
    if status is None:
        # Backplane down: fall back to in-process manager so the CLI is
        # still useful for "what's on disk" inspection.
        from .runtime.features.integrations import manager

        _emit_list(manager.list_integrations(), as_json=_as_json(args))
        return 0
    if status >= 400 or data is None:
        print(f"list failed: {err or status}", file=sys.stderr)
        return 1
    _emit_list(data, as_json=_as_json(args))
    return 0


def _read_file_arg(value: Optional[str]) -> Optional[str]:
    """``@-prefix`` means read from a file, ``-`` means stdin, else literal."""
    if value is None:
        return None
    if value == "-":
        return sys.stdin.read()
    if value.startswith("@"):
        return Path(value[1:]).expanduser().read_text(encoding="utf-8")
    return value


def _cmd_install(args: argparse.Namespace) -> int:
    handler_py = _read_file_arg(args.handler_py)
    init_py = _read_file_arg(args.init_py)
    yaml_text = _read_file_arg(args.yaml)

    from .runtime.features.integrations import manager

    try:
        result = manager.install(
            args.name,
            handler_py=handler_py,
            init_py=init_py,
            yaml=yaml_text,
            from_path=args.from_path,
            overwrite=args.overwrite,
        )
    except manager.IntegrationError as exc:
        print(f"install failed: {exc}", file=sys.stderr)
        return 1

    # Files written. Now ask the backplane to register/reload if alive.
    reload_info: Dict[str, Any] = {"reloaded": False, "reason": "backplane not running"}
    if _backplane_alive():
        status, data, err = _http(
            "POST", f"/hermes/integrations/reload?name={args.name}"
        )
        if status == 200 and data is not None:
            reload_info = {
                "reloaded": True,
                **{k: data.get(k) for k in ("mount", "path", "meta")},
            }
        else:
            reload_info = {"reloaded": False, "reason": err or f"HTTP {status}"}

    payload = {**result, "live": reload_info}
    _emit(payload, as_json=_as_json(args))
    if not _as_json(args) and not reload_info["reloaded"]:
        print(
            f"  (changes will apply on next backplane start: "
            f"{reload_info['reason']})",
            file=sys.stderr,
        )
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    if _backplane_alive():
        # Prefer the live path: backplane atomically unregisters routes
        # AND deletes the files. Keeps the live view consistent with disk.
        status, data, err = _http("DELETE", f"/hermes/integrations/{args.name}")
        if status == 200 and data is not None:
            _emit(data, as_json=_as_json(args))
            return 0
        if status is not None:
            print(f"remove failed: {err or status}", file=sys.stderr)
            return 1
        # status is None → reachability flickered; fall through to local

    from .runtime.features.integrations import manager

    try:
        result = manager.remove(args.name)
    except manager.IntegrationError as exc:
        print(f"remove failed: {exc}", file=sys.stderr)
        return 1
    _emit(result, as_json=_as_json(args))
    return 0


def _cmd_reload(args: argparse.Namespace) -> int:
    if not _backplane_alive():
        print(
            "reload needs a running backplane (it re-imports + swaps the "
            "live router). Start Hermes first, or just edit files and "
            "restart.",
            file=sys.stderr,
        )
        return 2
    status, data, err = _http("POST", f"/hermes/integrations/reload?name={args.name}")
    if status == 200 and data is not None:
        _emit(data, as_json=_as_json(args))
        return 0
    print(f"reload failed: {err or status}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Hermes CLI integration
# ---------------------------------------------------------------------------


def register_subparser(parser: argparse.ArgumentParser) -> None:
    """Populate the ``hermes integration`` subparser.

    Called by Hermes once at plugin load time. The shape:

        hermes integration [--json] {list|install|remove|reload} ...

    Each sub-subparser sets its own ``func`` via ``set_defaults`` so the
    Hermes CLI dispatcher invokes it directly.
    """
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    sub = parser.add_subparsers(dest="integration_cmd")

    p_list = sub.add_parser("list", help="show what's currently registered")
    p_list.set_defaults(func=_cmd_list)

    p_install = sub.add_parser(
        "install", help="write integration files + register if backplane is up"
    )
    p_install.add_argument(
        "name", help="URL-safe name; matches ^[a-z][a-z0-9-]*$"
    )
    p_install.add_argument(
        "--from-path",
        help=(
            "copy this directory verbatim into ~/.hermes/integrations/<name>/ "
            "(takes precedence over --handler-py / --init-py / --yaml)"
        ),
    )
    p_install.add_argument(
        "--handler-py",
        help=(
            "contents of handler.py; literal text, '@path' to read from a "
            "file, or '-' to read from stdin"
        ),
    )
    p_install.add_argument(
        "--init-py",
        help="contents of __init__.py; defaults to 'from .handler import setup'",
    )
    p_install.add_argument(
        "--yaml", help="contents of integration.yaml (metadata); optional"
    )
    p_install.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing integration with the same name",
    )
    p_install.set_defaults(func=_cmd_install)

    p_remove = sub.add_parser("remove", help="delete files + unregister routes")
    p_remove.add_argument("name")
    p_remove.set_defaults(func=_cmd_remove)

    p_reload = sub.add_parser(
        "reload", help="re-import + atomically swap the live router"
    )
    p_reload.add_argument("name")
    p_reload.set_defaults(func=_cmd_reload)


def run(args: argparse.Namespace) -> int:
    """Fallback handler when ``hermes integration`` is invoked without a sub-subcommand.

    With nothing to do, print a hint to stderr and exit non-zero so
    shell scripts can detect the no-op.
    """
    if getattr(args, "integration_cmd", None):
        # set_defaults(func=...) on the sub-subparsers takes precedence,
        # so reaching here with a chosen sub-subcommand means the dispatcher
        # didn't route — defensive log + non-zero exit.
        print(
            f"hermes integration: internal dispatch miss for "
            f"{args.integration_cmd!r}",
            file=sys.stderr,
        )
        return 2
    print(
        "hermes integration: no subcommand given. "
        "Try `hermes integration --help` to see list / install / remove / reload.",
        file=sys.stderr,
    )
    return 2
