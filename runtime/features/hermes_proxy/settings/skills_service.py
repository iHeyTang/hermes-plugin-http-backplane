"""Read-only enumeration of skills available to the current Hermes Agent.

Thin wrapper around upstream skill discovery + disabled-state config:

  * ``agent.skill_utils.iter_skill_index_files`` — walks ``$HERMES_HOME/skills``
    and external dirs for ``SKILL.md`` files.
  * ``agent.skill_utils.get_external_skills_dirs`` — expands
    ``skills.external_dirs`` from ``config.yaml``.
  * ``tools.skills_tool._parse_frontmatter`` / ``skill_matches_platform`` —
    YAML frontmatter parse + platform compatibility filter.
  * ``hermes_cli.skills_config.get_disabled_skills`` / ``save_disabled_skills``
    — the canonical disabled-set read/write.
  * ``hermes_cli.config.load_config`` — config.yaml read with schema-aware
    defaults.

This module supplies what those upstream pieces don't expose at all on the
HTTP surface: provenance (``origin``: bundled / hub / agent / manual / external),
timestamps (``created_at`` / ``updated_at`` / ``timestamp_source``), tag
extraction, plus the per-skill directory browser used by the options page.

Field shape matches the upstream ``/api/skills`` route where it exists
(``name`` / ``description`` / ``category`` / ``enabled``) — see the upstream
`hermes_cli/web_server.py:2895` handler. We intentionally do NOT carry a
backwards-compat translation layer; the frontend reads the upstream-aligned
shape directly.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ....adapters.hermes_core import hermes_home

logger = logging.getLogger("my-browser-bridge")

EXCLUDED_SKILL_DIRS = frozenset((".git", ".github", ".hub", ".archive"))

MAX_DESCRIPTION_CHARS = 240


# ---------------------------------------------------------------------------
# Upstream-backed helpers — defensive imports so the bridge can still
# return something useful when running outside a fresh Hermes install.
# ---------------------------------------------------------------------------


def _load_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config  # type: ignore

        cfg = load_config()
        return cfg if isinstance(cfg, dict) else {}
    except Exception as exc:
        logger.warning("hermes_cli.config.load_config failed: %s", exc)
        return {}


def _load_disabled_skill_names() -> Set[str]:
    """Disabled skill set, sourced from the canonical config helper."""
    try:
        from hermes_cli.skills_config import get_disabled_skills  # type: ignore

        return set(get_disabled_skills(_load_config()))
    except Exception as exc:
        logger.warning("get_disabled_skills failed: %s", exc)
        return set()


def _iter_skill_md(root: Path):
    """Walk a skills dir for ``SKILL.md`` files via the upstream iterator."""
    try:
        from agent.skill_utils import iter_skill_index_files  # type: ignore

        yield from iter_skill_index_files(root, "SKILL.md")
    except Exception as exc:
        logger.warning("iter_skill_index_files unavailable: %s", exc)
        # Manual fallback so this module never silently returns []
        # just because Hermes isn't importable in this process.
        if not root.exists():
            return
        for current, dirs, files in os.walk(root, followlinks=True):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_SKILL_DIRS]
            if "SKILL.md" in files:
                yield Path(current) / "SKILL.md"


def _external_skills_dirs() -> List[Path]:
    try:
        from agent.skill_utils import get_external_skills_dirs  # type: ignore

        return [Path(p) for p in get_external_skills_dirs()]
    except Exception as exc:
        logger.warning("get_external_skills_dirs failed: %s", exc)
        return []


def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    try:
        from tools.skills_tool import _parse_frontmatter as _parser  # type: ignore

        fm, body = _parser(text)
        if not isinstance(fm, dict):
            fm = {}
        return fm, body
    except Exception:
        # Local fallback so callers always get a parsed-ish result.
        if not text.startswith("---"):
            return {}, text
        end = text.find("\n---", 3)
        if end == -1:
            return {}, text
        import yaml  # type: ignore

        try:
            parsed = yaml.safe_load(text[3:end].lstrip("\n")) or {}
        except Exception:  # noqa: BLE001
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        return parsed, text[end + 4 :].lstrip("\n")


def _matches_platform(frontmatter: Dict[str, Any]) -> bool:
    try:
        from tools.skills_tool import skill_matches_platform  # type: ignore

        return bool(skill_matches_platform(frontmatter))
    except Exception:
        # Permissive fallback — better to show the skill than hide it.
        return True


# ---------------------------------------------------------------------------
# Provenance + timestamp helpers — Hermes' upstream HTTP surface doesn't
# expose these, so the logic stays local.
# ---------------------------------------------------------------------------


def _load_bundled_names() -> Set[str]:
    path = hermes_home() / "skills" / ".bundled_manifest"
    if not path.exists():
        return set()
    out: Set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            name = line.split(":", 1)[0].strip()
            if name:
                out.add(name)
    except OSError as exc:
        logger.debug("read .bundled_manifest: %s", exc)
    return out


def _load_usage_records() -> Dict[str, Dict[str, Any]]:
    path = hermes_home() / "skills" / ".usage.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("parse .usage.json: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(n): r for n, r in data.items() if isinstance(r, dict)}


def _load_hub_records() -> Dict[str, Dict[str, Any]]:
    path = hermes_home() / "skills" / ".hub" / "lock.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("parse hub/lock.json: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    installed = data.get("installed")
    if not isinstance(installed, dict):
        return {}
    return {str(n): r for n, r in installed.items() if isinstance(r, dict)}


def _fs_birth_iso(path: Path) -> Optional[str]:
    try:
        st = path.stat()
    except OSError:
        return None
    ts = getattr(st, "st_birthtime", None)
    if ts is None:
        ts = st.st_ctime
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OSError, ValueError, OverflowError):
        return None


def _fs_mtime_iso(path: Path) -> Optional[str]:
    try:
        st = path.stat()
    except OSError:
        return None
    try:
        return datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    except (OSError, ValueError, OverflowError):
        return None


def _as_iso(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _resolve_timestamps(
    name: str,
    skill_md: Path,
    usage: Dict[str, Dict[str, Any]],
    hub: Dict[str, Dict[str, Any]],
) -> Tuple[Optional[str], Optional[str], str]:
    u = usage.get(name) or {}
    h = hub.get(name) or {}
    created = (
        _as_iso(u.get("created_at"))
        or _as_iso(h.get("installed_at"))
        or _fs_birth_iso(skill_md)
    )
    updated = (
        _as_iso(u.get("last_patched_at"))
        or _as_iso(h.get("updated_at"))
        or _fs_mtime_iso(skill_md)
    )
    if u.get("created_at") or u.get("last_patched_at"):
        source = "usage"
    elif h.get("installed_at") or h.get("updated_at"):
        source = "hub"
    else:
        source = "fs"
    return created, updated, source


def _classify_origin(
    name: str,
    is_external: bool,
    bundled: Set[str],
    hub_names: Set[str],
    agent_names: Set[str],
) -> str:
    if is_external:
        return "external"
    if name in agent_names:
        return "agent"
    if name in hub_names:
        return "hub"
    if name in bundled:
        return "bundled"
    return "manual"


def _extract_tags(frontmatter: Dict[str, Any]) -> List[str]:
    meta = frontmatter.get("metadata")
    if not isinstance(meta, dict):
        return []
    hermes_meta = meta.get("hermes")
    if not isinstance(hermes_meta, dict):
        return []
    raw = hermes_meta.get("tags")
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [str(t).strip() for t in raw if str(t).strip()][:32]


def _extract_description(frontmatter: Dict[str, Any], body: str) -> str:
    desc = frontmatter.get("description")
    if isinstance(desc, str) and desc.strip():
        result = desc.strip()
    else:
        result = ""
        for line in body.strip().splitlines():
            ln = line.strip()
            if ln and not ln.startswith("#"):
                result = ln
                break
    if len(result) > MAX_DESCRIPTION_CHARS:
        result = result[: MAX_DESCRIPTION_CHARS - 3] + "..."
    return result


def _category_from_path(skill_md: Path, root: Path) -> Optional[str]:
    try:
        rel = skill_md.relative_to(root)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) <= 2:
        return None
    return parts[0]


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def list_skills() -> Dict[str, Any]:
    """Enumerate all skills visible to Hermes Agent.

    Output shape (per skill):
      - ``name`` / ``description`` / ``category`` — upstream-aligned
      - ``enabled`` (boolean) — upstream ``/api/skills`` ``enabled`` field;
        true when the skill is platform-compatible AND not in the disabled set
      - ``path`` (string) — absolute path to ``SKILL.md`` (used by the
        options page's per-skill file viewer; upstream doesn't expose this)
      - ``origin`` — ``bundled``/``hub``/``agent``/``manual``/``external``
      - ``platforms`` / ``version`` / ``tags`` / ``created_at`` /
        ``updated_at`` / ``timestamp_source`` — supplementary metadata
    """
    disabled = _load_disabled_skill_names()
    bundled_names = _load_bundled_names()
    hub_records = _load_hub_records()
    usage_records = _load_usage_records()
    hub_names = set(hub_records.keys())
    agent_names = {
        name
        for name, rec in usage_records.items()
        if rec.get("created_by") == "agent" or rec.get("agent_created") is True
    }

    local_root = hermes_home() / "skills"
    scan_roots: List[Tuple[Path, bool]] = [(local_root, False)]
    for ext_dir in _external_skills_dirs():
        scan_roots.append((ext_dir, True))

    skills: List[Dict[str, Any]] = []
    seen_names: Set[str] = set()

    for root, is_external in scan_roots:
        for skill_md in _iter_skill_md(root):
            if any(part in EXCLUDED_SKILL_DIRS for part in skill_md.parts):
                continue
            try:
                head = skill_md.read_text(encoding="utf-8")[:4096]
            except (OSError, UnicodeDecodeError) as exc:
                logger.debug("skip %s: %s", skill_md, exc)
                continue

            fm, body = _parse_frontmatter(head)
            if not _matches_platform(fm):
                continue

            name = str(fm.get("name") or skill_md.parent.name).strip()
            if not name or name in seen_names:
                continue
            seen_names.add(name)

            origin = _classify_origin(
                name, is_external, bundled_names, hub_names, agent_names
            )
            created_at, updated_at, ts_source = _resolve_timestamps(
                name, skill_md, usage_records, hub_records
            )

            skills.append(
                {
                    "name": name,
                    "description": _extract_description(fm, body),
                    "category": _category_from_path(skill_md, root),
                    "enabled": name not in disabled,
                    "path": str(skill_md),
                    "origin": origin,
                    "platforms": fm.get("platforms")
                    if isinstance(fm.get("platforms"), list)
                    else None,
                    "version": str(fm.get("version") or "").strip() or None,
                    "tags": _extract_tags(fm),
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "timestamp_source": ts_source,
                }
            )

    skills.sort(key=lambda s: ((s.get("category") or "~"), s["name"].lower()))

    total = len(skills)
    enabled_count = sum(1 for s in skills if s["enabled"])
    origin_counts: Dict[str, int] = {}
    for s in skills:
        origin_counts[s["origin"]] = origin_counts.get(s["origin"], 0) + 1

    return {
        "skills": skills,
        "skills_dirs": [str(p) for p, _ in scan_roots],
        "totals": {
            "total": total,
            "enabled": enabled_count,
            "disabled": total - enabled_count,
        },
        "origin_counts": origin_counts,
    }


def list_skills_response() -> Dict[str, Any]:
    return {"ok": True, **list_skills()}


def toggle_skill(name: str, enabled: bool) -> Dict[str, Any]:
    """Persist a skill's enabled state by mutating ``approvals.skills.disabled``.

    Routes the actual write through the upstream
    ``hermes_cli.skills_config.save_disabled_skills`` helper, which is the
    same path the dashboard's ``PUT /api/skills/toggle`` uses. We accept a
    plain ``enabled`` boolean to match the symmetry of GET ``/hermes/skills``
    (which carries ``enabled``); upstream's body shape is the same.
    """
    if not isinstance(name, str) or not name.strip():
        return {"ok": False, "error": "name is required"}
    name = name.strip()
    try:
        from hermes_cli.skills_config import (  # type: ignore
            get_disabled_skills,
            save_disabled_skills,
        )

        config = _load_config()
        disabled = set(get_disabled_skills(config))
        if enabled:
            disabled.discard(name)
        else:
            disabled.add(name)
        save_disabled_skills(config, disabled)
        return {"ok": True, "name": name, "enabled": enabled}
    except Exception as exc:
        logger.exception("toggle_skill failed for %s", name)
        return {"ok": False, "error": f"toggle failed: {exc}"}


# ---------------------------------------------------------------------------
# Skill directory browsing — used by the options page "view files" affordance.
# Read-only: we never mutate the skill tree from the bridge. Upstream
# (`/api/skills`) has no equivalent so the logic stays here.
# ---------------------------------------------------------------------------

# Cap file enumeration so a stray symlink loop or an enormous external dir
# can't wedge the bridge. 10k is far above any sane skill.
MAX_SKILL_FILES = 10_000

# Cap on a single file read. Anything past this is reported as too-large
# instead of streamed; the options page is a viewer, not an IDE.
MAX_SKILL_FILE_BYTES = 1 * 1024 * 1024  # 1 MiB


def _resolve_skill_dir(name: str) -> Optional[Tuple[Path, Dict[str, Any]]]:
    """Find the on-disk directory for a skill by name.

    Returns ``(skill_dir, entry)`` where ``skill_dir`` is the parent of the
    skill's ``SKILL.md`` and ``entry`` is the metadata row from ``list_skills``.
    Returns ``None`` if no skill with that name is visible.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    target = name.strip()
    for entry in list_skills()["skills"]:
        if entry.get("name") == target:
            skill_md = Path(str(entry.get("path") or ""))
            if not skill_md.is_file():
                return None
            return skill_md.parent, entry
    return None


def list_skill_files(name: str) -> Dict[str, Any]:
    """Walk a skill's directory and return one flat record per file.

    Output shape::

        {
            "ok": True,
            "name": "<skill name>",
            "root": "<absolute path of the skill dir>",
            "files": [
                {"path": "SKILL.md", "size": 1234, "modified_at": "..."},
                {"path": "references/foo.md", "size": 567, "modified_at": "..."},
                ...
            ],
            "truncated": False,
        }

    Directory entries are NOT returned — the UI groups by splitting `path`
    on ``/``. Excludes the same scaffolding dirs as the skill scanner
    (``.git``, ``.hub``, ...).
    """
    resolved = _resolve_skill_dir(name)
    if resolved is None:
        return {"ok": False, "error": f"skill {name!r} not found"}
    skill_dir, _entry = resolved
    if not skill_dir.is_dir():
        return {"ok": False, "error": f"skill directory missing: {skill_dir}"}

    files: List[Dict[str, Any]] = []
    truncated = False
    for current, dirs, names in os.walk(skill_dir, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_SKILL_DIRS)
        names.sort()
        for fname in names:
            if len(files) >= MAX_SKILL_FILES:
                truncated = True
                break
            full = Path(current) / fname
            try:
                st = full.stat()
            except OSError:
                continue
            try:
                rel = full.relative_to(skill_dir).as_posix()
            except ValueError:
                continue
            mod_iso: Optional[str]
            try:
                mod_iso = datetime.fromtimestamp(
                    st.st_mtime, tz=timezone.utc
                ).isoformat()
            except (OSError, ValueError, OverflowError):
                mod_iso = None
            files.append(
                {
                    "path": rel,
                    "size": int(st.st_size),
                    "modified_at": mod_iso,
                }
            )
        if truncated:
            break

    files.sort(key=lambda f: f["path"])
    return {
        "ok": True,
        "name": _entry.get("name"),
        "root": str(skill_dir),
        "files": files,
        "truncated": truncated,
    }


def _is_probably_binary(sample: bytes) -> bool:
    return b"\x00" in sample


def read_skill_file(name: str, rel_path: str) -> Dict[str, Any]:
    """Read one file under a skill's directory.

    Path-traversal safe: the resolved file MUST live under the skill root,
    or we refuse. Binary files are returned with ``encoding: "binary"`` and
    no body (the UI shows a "binary, X bytes" placeholder). Files larger
    than ``MAX_SKILL_FILE_BYTES`` get ``encoding: "too-large"`` for the
    same reason.
    """
    resolved = _resolve_skill_dir(name)
    if resolved is None:
        return {"ok": False, "error": f"skill {name!r} not found"}
    skill_dir, _entry = resolved
    if not isinstance(rel_path, str) or not rel_path.strip():
        return {"ok": False, "error": "path is required"}

    candidate = (skill_dir / rel_path).resolve()
    # Resolve once: skills are commonly installed as symlinks (e.g. lark-* →
    # ~/.agents/skills/*), so `candidate` (already resolved) won't be
    # relative to the unresolved `skill_dir`.
    skill_root = skill_dir.resolve()
    try:
        rel = candidate.relative_to(skill_root)
    except ValueError:
        return {"ok": False, "error": "path escapes skill directory"}
    if any(part in EXCLUDED_SKILL_DIRS for part in rel.parts):
        return {"ok": False, "error": "path is inside an excluded directory"}
    if not candidate.is_file():
        return {"ok": False, "error": f"not a regular file: {rel_path}"}

    try:
        st = candidate.stat()
    except OSError as exc:
        return {"ok": False, "error": f"stat failed: {exc}"}

    size = int(st.st_size)
    if size > MAX_SKILL_FILE_BYTES:
        return {
            "ok": True,
            "name": _entry.get("name"),
            "path": rel_path,
            "size": size,
            "encoding": "too-large",
            "limit": MAX_SKILL_FILE_BYTES,
            "content": None,
        }

    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        return {"ok": False, "error": f"read failed: {exc}"}

    if _is_probably_binary(raw[:8192]):
        return {
            "ok": True,
            "name": _entry.get("name"),
            "path": rel_path,
            "size": size,
            "encoding": "binary",
            "content": None,
        }

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1")
        except UnicodeDecodeError:
            return {
                "ok": True,
                "name": _entry.get("name"),
                "path": rel_path,
                "size": size,
                "encoding": "binary",
                "content": None,
            }

    return {
        "ok": True,
        "name": _entry.get("name"),
        "path": rel_path,
        "size": size,
        "encoding": "utf-8",
        "content": text,
    }
