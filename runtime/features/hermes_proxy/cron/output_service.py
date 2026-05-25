"""Read access to cron job run outputs.

Hermes writes one markdown file per cron run to
``$HERMES_HOME/cron/output/{job_id}/{YYYY-MM-DD_HH-MM-SS}.md``. The bridge
exposes those as a queryable index over HTTP — that's the entire purpose
of this module. It does not own any storage and does not know what the
consumer renders the output as.

The parser stays defensive — Hermes could change its output format
upstream, and we'd rather degrade to a generic "completed at T" entry
than crash the consumer. Anything we can't recognise falls through to
``status="ok"`` with the raw body as the content.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ....adapters.hermes_core import hermes_home

logger = logging.getLogger("my-browser-bridge")

# Filename shape: 2026-05-14_10-16-10.md
_RUN_FILE_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})\.md$"
)

# First-line header: "# Cron Job: <name>" with optional " (FAILED)" suffix.
_HEADER_RE = re.compile(
    r"^#\s+Cron Job:\s*(?P<name>.*?)(?:\s*\((?P<flag>[A-Z]+)\))?\s*$"
)

# Hard cap on a single run file we'll read into memory for indexing — guards
# against a runaway agent dumping 50 MB of debug into one output. A truncated
# read still produces a valid card; the user just sees the head of the run.
_MAX_RUN_FILE_BYTES = 256 * 1024


def _output_root() -> Path:
    return hermes_home() / "cron" / "output"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


@dataclass
class _ParsedRun:
    job_id: str
    run_id: str
    run_at_ms: int
    job_name: str
    status: str  # "ok" | "error" | "silent"
    # Full markdown body — the Response section for ok runs, the Error
    # section for failures, or a canned line for silent runs. Never
    # truncated by the bridge; clipped only when the file itself is
    # absurdly large (``_MAX_RUN_FILE_BYTES``).
    content: str
    raw_size_bytes: int
    # Whether ``content`` was clipped because the source file exceeded
    # ``_MAX_RUN_FILE_BYTES``. UI can surface a "file too large to fully
    # load" hint when set.
    truncated_by_size: bool


def _parse_run_id(run_id: str) -> Optional[int]:
    """Convert ``2026-05-14_10-16-10`` → epoch ms in the local timezone.

    Cron writes timestamps in the user's local time (filename is generated
    by ``datetime.now().strftime(...)`` upstream), so we mirror that — use
    ``astimezone()`` with no argument to attach the local zone.
    """
    m = _RUN_FILE_RE.fullmatch(run_id + ".md")
    if not m:
        return None
    try:
        dt = datetime(
            int(m.group(1)),
            int(m.group(2)),
            int(m.group(3)),
            int(m.group(4)),
            int(m.group(5)),
            int(m.group(6)),
        ).astimezone()
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


# The fixed set of top-level sections Hermes core writes into a cron
# output file. Anything else that looks like an H2 (``## …``) inside a
# section's body is *content* — the agent's response often has its own
# ``## 一、概览`` style headings, and treating those as section breaks
# silently swallows the rest of the report. We anchor on this allowlist
# instead of any-H2-line so the parser stays robust to free-form bodies.
_TOP_SECTION_NAMES = frozenset({"prompt", "response", "error", "script"})

_SECTION_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$")


def _split_sections(text: str) -> Tuple[str, Dict[str, str]]:
    """Split a run markdown into (header_block, sections).

    Sections are keyed by their H2 title (e.g. ``"Response"``, ``"Error"``,
    ``"Prompt"``). Only the canonical top-level names in
    ``_TOP_SECTION_NAMES`` are treated as section boundaries — H2 lines
    inside a section's body are kept as part of that body, so a report
    with its own ``## 概览`` / ``## 详情`` headings round-trips intact.
    The header block is everything before the first recognised section.
    """
    lines = text.splitlines()
    sections: Dict[str, str] = {}
    header_lines: List[str] = []
    cur_key: Optional[str] = None
    cur_buf: List[str] = []
    seen_any_section = False

    for line in lines:
        m = _SECTION_HEADER_RE.match(line)
        title = m.group(1).strip() if m else ""
        if title and title.lower() in _TOP_SECTION_NAMES:
            if cur_key is not None:
                sections[cur_key] = "\n".join(cur_buf).strip()
            elif not seen_any_section:
                header_lines = list(cur_buf)
            cur_key = title
            cur_buf = []
            seen_any_section = True
        else:
            cur_buf.append(line)
    if cur_key is not None:
        sections[cur_key] = "\n".join(cur_buf).strip()
    elif not seen_any_section:
        header_lines = list(cur_buf)
    return "\n".join(header_lines), sections


def _parse_run_file(
    job_id: str, run_id: str, path: Path
) -> Optional[_ParsedRun]:
    try:
        stat = path.stat()
    except OSError:
        return None
    truncated_by_size = False
    if stat.st_size > _MAX_RUN_FILE_BYTES:
        truncated_by_size = True
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")[
                : _MAX_RUN_FILE_BYTES
            ]
        except OSError:
            return None
    else:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    run_at_ms = _parse_run_id(run_id)
    if run_at_ms is None:
        # Filename didn't match the expected pattern; fall back to mtime.
        run_at_ms = int(stat.st_mtime * 1000)

    header, sections = _split_sections(raw)

    # Default job_name: the first H1 we can find; fallback to job_id.
    job_name = job_id
    failed_flag = False
    for line in header.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            name = m.group("name").strip()
            if name:
                job_name = name
            flag = (m.group("flag") or "").upper()
            if flag in ("FAILED", "ERROR"):
                failed_flag = True
            break

    response_full: Optional[str] = sections.get("Response")
    error_full: Optional[str] = sections.get("Error")

    if failed_flag or error_full:
        status = "error"
        content = (error_full or response_full or "Cron run failed.").strip()
    elif response_full is not None:
        body = response_full.strip()
        # Hermes uses literal "[SILENT]" to mark "agent had nothing to report".
        if body == "[SILENT]":
            status = "silent"
            content = "Hermes checked in — nothing new to report."
        else:
            status = "ok"
            content = body
    else:
        # No recognised sections; treat as ok with the raw body.
        status = "ok"
        content = raw.strip()

    return _ParsedRun(
        job_id=job_id,
        run_id=run_id,
        run_at_ms=run_at_ms,
        job_name=job_name,
        status=status,
        content=content,
        raw_size_bytes=stat.st_size,
        truncated_by_size=truncated_by_size,
    )


def _serialise_run(p: _ParsedRun) -> Dict[str, Any]:
    return {
        "job_id": p.job_id,
        "run_id": p.run_id,
        "run_at_ms": p.run_at_ms,
        "job_name": p.job_name,
        "status": p.status,
        "content": p.content,
        "size_bytes": p.raw_size_bytes,
        "truncated_by_size": p.truncated_by_size,
    }


# ---------------------------------------------------------------------------
# Index / detail
# ---------------------------------------------------------------------------


def _iter_run_files() -> List[Tuple[str, str, Path]]:
    """Yield ``(job_id, run_id, path)`` for every run file we can see."""
    root = _output_root()
    if not root.is_dir():
        return []
    out: List[Tuple[str, str, Path]] = []
    for job_dir in root.iterdir():
        if not job_dir.is_dir():
            continue
        job_id = job_dir.name
        for run_file in job_dir.iterdir():
            if not run_file.is_file() or not run_file.name.endswith(".md"):
                continue
            run_id = run_file.name[: -len(".md")]
            out.append((job_id, run_id, run_file))
    return out


def list_recent_runs(
    *,
    since_ms: Optional[int] = None,
    limit: int = 100,
    include_silent: bool = True,
) -> Dict[str, Any]:
    """Return the most recent cron runs across all jobs.

    Newest first, capped at ``limit``. ``since_ms`` filters out runs
    whose run timestamp is at or before the cursor — clients pass their
    last-seen timestamp for incremental polling.

    Each entry carries the full ``content`` body, not a preview — the
    consumer renders verbatim and decides client-side how much to fold.
    """
    limit = max(1, min(500, int(limit or 100)))
    files = _iter_run_files()
    parsed: List[_ParsedRun] = []
    for job_id, run_id, path in files:
        p = _parse_run_file(job_id, run_id, path)
        if p is None:
            continue
        if since_ms is not None and p.run_at_ms <= since_ms:
            continue
        if not include_silent and p.status == "silent":
            continue
        parsed.append(p)

    parsed.sort(key=lambda x: x.run_at_ms, reverse=True)
    truncated = len(parsed) > limit
    runs = parsed[:limit]
    return {
        "ok": True,
        "runs": [_serialise_run(p) for p in runs],
        "truncated": truncated,
        "total": len(parsed),
    }


def get_run(job_id: str, run_id: str) -> Dict[str, Any]:
    """Return the full content of a single cron run.

    Same payload shape as one entry in ``list_recent_runs`` — the
    index already carries everything; this endpoint exists for direct
    lookup by ``(job_id, run_id)``.
    """
    if not job_id or not run_id:
        return {"ok": False, "error": "job_id and run_id required"}
    # Defend against ``..`` and absolute paths in user-controlled segments.
    if "/" in job_id or "\\" in job_id or job_id.startswith("."):
        return {"ok": False, "error": "invalid job_id"}
    if "/" in run_id or "\\" in run_id or run_id.startswith("."):
        return {"ok": False, "error": "invalid run_id"}

    path = _output_root() / job_id / f"{run_id}.md"
    if not path.is_file():
        return {"ok": False, "error": "run not found"}
    p = _parse_run_file(job_id, run_id, path)
    if p is None:
        return {"ok": False, "error": "failed to parse run file"}
    return {"ok": True, "run": _serialise_run(p)}
