"""
Attachment upload service.

Owns both the request-level validation and the on-disk persistence layout
under ``<hermes_home>/plugins/<plugin>/attachments/<session>/``. The plugin
has no other use for attachment storage, so there is no separate adapter.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Any, Dict, Optional

from ....adapters.hermes_core import hermes_home

PLUGIN_NAME = "hermes-plugin-http-backplane"
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SESSION_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _attachments_root() -> Path:
    root = hermes_home() / "plugins" / PLUGIN_NAME / "attachments"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _attachment_session_dir(session_id: Optional[str]) -> Path:
    safe = _SESSION_ID_SAFE_RE.sub("_", str(session_id or "default")).strip("_")
    if not safe:
        safe = "default"
    p = _attachments_root() / safe
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_basename(raw: str) -> str:
    base = os.path.basename(raw or "").strip()
    if not base or base in (".", ".."):
        return "file"
    cleaned = _FILENAME_SAFE_RE.sub("_", base).strip("._-")
    return cleaned or "file"


def _save_attachment(
    session_id: Optional[str], name: str, mime: str, data: bytes
) -> Dict[str, Any]:
    name = _safe_basename(name)
    mime = (mime or "application/octet-stream").strip() or "application/octet-stream"
    session_dir = _attachment_session_dir(session_id)
    uid = secrets.token_hex(4)
    target = session_dir / f"{uid}_{name}"
    target.write_bytes(data)
    return {
        "ok": True,
        "path": str(target),
        "name": name,
        "mime": mime,
        "size": len(data),
    }


def build_attachment_upload_response(
    *,
    session_id: Optional[str],
    name: str,
    mime: str,
    content_length: Optional[int],
    data: bytes,
) -> Dict[str, Any]:
    cl = int(content_length or 0)
    if cl <= 0:
        raise ValueError("Content-Length required")
    if cl > MAX_ATTACHMENT_BYTES:
        raise OverflowError("attachment too large")
    if not data:
        raise ValueError("empty body")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise OverflowError("attachment too large")
    return _save_attachment(session_id=session_id, name=name, mime=mime, data=data)


def delete_attachment(path: str) -> Dict[str, Any]:
    """Best-effort deletion of a previously-uploaded attachment file.

    Refuses any path that doesn't resolve under the attachments root, so a
    malformed client request can't ask us to unlink arbitrary disk paths.
    """
    if not path:
        return {"deleted": False, "reason": "missing path"}
    target = Path(path).resolve()
    root = _attachments_root().resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise PermissionError(f"refused: path outside attachments root: {target}")
    if target.exists():
        try:
            if target.is_dir():
                for child in target.iterdir():
                    if child.is_file():
                        child.unlink()
                target.rmdir()
            else:
                target.unlink()
        except OSError as exc:
            return {"deleted": False, "reason": str(exc)}
        return {"deleted": True}
    return {"deleted": False, "reason": "not found"}


def delete_attachment_session(session_id: str) -> Dict[str, Any]:
    """Wipe every attachment uploaded for a chat session."""
    if not session_id:
        return {"deleted": False, "reason": "missing session_id"}
    safe = _SESSION_ID_SAFE_RE.sub("_", session_id).strip("_") or "default"
    target = (_attachments_root() / safe).resolve()
    return delete_attachment(str(target))
