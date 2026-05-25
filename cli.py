"""``hermes-integration`` — operator-facing CLI for backplane integrations.

Lives independently of the running Hermes agent: file work happens
locally against ``~/.hermes/integrations/``; when the backplane HTTP
server is reachable on the local port, the file-changing subcommands
also trigger a live re-register so changes take effect immediately.
Backplane down → file work still applies on the next backplane start,
and the CLI prints a one-liner to that effect.

The CLI does NOT depend on the Hermes agent package or its plugin
registry. It imports the backplane's :mod:`runtime.features.integrations.manager`
for in-process operations (used when the backplane is down) and talks
HTTP to the running backplane otherwise.

Subcommands:
    list              snapshot of what's currently registered
    install <name>    write files + (if backplane is up) re-register
    remove <name>     delete files + (if backplane is up) unregister
    reload <name>     re-import + atomically swap router (needs backplane)
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
# Subcommand implementations
# ---------------------------------------------------------------------------


def _cmd_list(args: argparse.Namespace) -> int:
    status, data, err = _http("GET", "/hermes/integrations")
    if status is None:
        # Backplane down: fall back to local manager so the CLI is still
        # useful for "what's on disk" inspection.
        from .runtime.features.integrations import manager

        _emit_list(manager.list_integrations(), as_json=args.json)
        return 0
    if status >= 400 or data is None:
        print(f"list failed: {err or status}", file=sys.stderr)
        return 1
    _emit_list(data, as_json=args.json)
    return 0


def _read_file_arg(value: Optional[str]) -> Optional[str]:
    """``@-prefix`` means read from a file, else literal string. Sentinel ``-`` → stdin."""
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
            reload_info = {"reloaded": True, **{k: data.get(k) for k in ("mount", "path", "meta")}}
        else:
            reload_info = {"reloaded": False, "reason": err or f"HTTP {status}"}

    payload = {**result, "live": reload_info}
    _emit(payload, as_json=args.json)
    if not args.json and not reload_info["reloaded"]:
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
            _emit(data, as_json=args.json)
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
    _emit(result, as_json=args.json)
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
        _emit(data, as_json=args.json)
        return 0
    print(f"reload failed: {err or status}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hermes-integration",
        description=(
            "Manage user-installed Hermes backplane integrations "
            "(~/.hermes/integrations/<name>/). Operator-facing — not "
            "intended for agent invocation."
        ),
    )
    p.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show what's currently registered")

    inst = sub.add_parser(
        "install", help="write integration files + register if backplane is up"
    )
    inst.add_argument("name", help="URL-safe name; matches ^[a-z][a-z0-9-]*$")
    inst.add_argument(
        "--from-path",
        help=(
            "copy this directory verbatim into ~/.hermes/integrations/<name>/ "
            "(takes precedence over --handler-py / --init-py / --yaml)"
        ),
    )
    inst.add_argument(
        "--handler-py",
        help=(
            "contents of handler.py; literal text, '@path' to read from a "
            "file, or '-' to read from stdin"
        ),
    )
    inst.add_argument(
        "--init-py",
        help="contents of __init__.py; defaults to 'from .handler import setup'",
    )
    inst.add_argument(
        "--yaml", help="contents of integration.yaml (metadata); optional"
    )
    inst.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing integration with the same name",
    )

    rm = sub.add_parser("remove", help="delete files + unregister routes")
    rm.add_argument("name")

    rel = sub.add_parser(
        "reload", help="re-import + atomically swap the live router"
    )
    rel.add_argument("name")

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    dispatch = {
        "list": _cmd_list,
        "install": _cmd_install,
        "remove": _cmd_remove,
        "reload": _cmd_reload,
    }
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
