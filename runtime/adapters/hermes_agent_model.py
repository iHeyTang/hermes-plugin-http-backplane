"""
Read/write Hermes CLI model blocks in ~/.hermes/config.yaml.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .hermes_core import hermes_home


def read_config_provider_keys() -> List[str]:
    path = _config_yaml_path()
    if not path.exists():
        return []
    try:
        import yaml  # type: ignore
    except ImportError:
        return []
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    if not isinstance(cfg, dict):
        return []
    p = cfg.get("providers")
    if not isinstance(p, dict):
        return []
    return sorted(str(k) for k in p.keys() if isinstance(k, str) and str(k).strip())


def _config_yaml_path() -> Path:
    return hermes_home() / "config.yaml"


def _load_yaml_module():
    try:
        import yaml  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "PyYAML is required to edit Hermes config from the bridge. Install: pip install pyyaml"
        ) from e
    return yaml


def _resolve_context_lengths(
    *, model: str, provider: str, base_url: Optional[str], config_ctx: Optional[int]
) -> Dict[str, int]:
    """Resolve auto-detected + effective context lengths via upstream.

    Mirrors what ``hermes_cli/web_server.py:/api/model/info`` does:
    consult ``agent.model_metadata.get_model_context_length`` for the
    auto value (independent of any config override), keep the
    config-supplied override separately so the UI can show both, and
    return ``effective = config or auto``. Returns zeros when upstream
    isn't importable so the HTTP shape stays consistent.
    """
    auto_ctx = 0
    if model:
        try:
            from agent.model_metadata import get_model_context_length  # type: ignore

            auto_ctx = int(
                get_model_context_length(
                    model=model,
                    base_url=base_url or "",
                    provider=provider or "",
                    config_context_length=None,
                )
                or 0
            )
        except Exception:
            auto_ctx = 0
    cfg_ctx = int(config_ctx) if isinstance(config_ctx, int) and config_ctx > 0 else 0
    effective = cfg_ctx if cfg_ctx > 0 else auto_ctx
    return {
        "auto_context_length": auto_ctx,
        "config_context_length": cfg_ctx,
        "effective_context_length": effective,
    }


def _resolve_capabilities(*, model: str, provider: str) -> Dict[str, Any]:
    """Best-effort capability lookup via ``agent.models_dev``.

    Field shape matches upstream's ``/api/model/info.capabilities`` block.
    Returns ``{}`` (not ``None``) when the model isn't known to
    ``models.dev`` so the frontend can treat absence as "unknown" rather
    than "unsupported".
    """
    if not model:
        return {}
    try:
        from agent.models_dev import get_model_capabilities  # type: ignore

        mc = get_model_capabilities(provider=provider or "", model=model)
        if mc is None:
            return {}
        return {
            "supports_tools": bool(getattr(mc, "supports_tools", False)),
            "supports_vision": bool(getattr(mc, "supports_vision", False)),
            "supports_reasoning": bool(getattr(mc, "supports_reasoning", False)),
            "context_window": getattr(mc, "context_window", None),
            "max_output_tokens": getattr(mc, "max_output_tokens", None),
            "model_family": getattr(mc, "model_family", None),
        }
    except Exception:
        return {}


def read_main_model() -> Dict[str, Any]:
    path = _config_yaml_path()
    if not path.exists():
        return {
            "config_path": str(path),
            "config_exists": False,
            "provider": "auto",
            "model": "",
            "base_url": None,
            "auto_context_length": 0,
            "config_context_length": 0,
            "effective_context_length": 0,
            "capabilities": {},
        }
    yaml = _load_yaml_module()
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return {
            "config_path": str(path),
            "config_exists": True,
            "error": str(e),
            "provider": "auto",
            "model": "",
            "base_url": None,
            "auto_context_length": 0,
            "config_context_length": 0,
            "effective_context_length": 0,
            "capabilities": {},
        }
    if not isinstance(cfg, dict):
        cfg = {}
    block = cfg.get("model")
    if not isinstance(block, dict):
        block = {}
    name = block.get("default")
    if name is None:
        name = block.get("model")
    if name is not None and not isinstance(name, str):
        name = str(name)
    prov = block.get("provider")
    if prov is None or prov == "":
        prov = "auto"
    elif not isinstance(prov, str):
        prov = str(prov)
    bu = block.get("base_url")
    if bu is not None and not isinstance(bu, str):
        bu = str(bu)
    if isinstance(bu, str) and not bu.strip():
        bu = None
    config_ctx_raw = block.get("context_length")
    ctx = _resolve_context_lengths(
        model=name or "",
        provider=prov,
        base_url=bu,
        config_ctx=config_ctx_raw if isinstance(config_ctx_raw, int) else None,
    )
    caps = _resolve_capabilities(model=name or "", provider=prov)
    return {
        "config_path": str(path),
        "config_exists": True,
        "provider": prov,
        "model": name or "",
        "base_url": bu,
        **ctx,
        "capabilities": caps,
    }


def write_main_model(
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    path = _config_yaml_path()
    yaml = _load_yaml_module()
    cfg: Dict[str, Any] = {}
    if path.exists():
        try:
            cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            raise ValueError(f"cannot parse existing config.yaml: {e}") from e
    if not isinstance(cfg, dict):
        cfg = {}
    mblock = cfg.get("model")
    if not isinstance(mblock, dict):
        mblock = {}
    mblock = dict(mblock)

    if provider is not None:
        p = str(provider).strip()
        mblock["provider"] = p if p else "auto"
    if model is not None:
        mid = str(model).strip()
        if mid:
            mblock["default"] = mid
            mblock.pop("model", None)
        else:
            mblock.pop("default", None)
            mblock.pop("model", None)
    if base_url is not None:
        bu = str(base_url).strip()
        if bu:
            mblock["base_url"] = bu
        else:
            mblock.pop("base_url", None)

    cfg["model"] = mblock
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False)
    path.write_text(text, encoding="utf-8")
    return read_main_model()


AUXILIARY_SLOTS: List[str] = [
    "vision",
    "web_extract",
    "compression",
    "session_search",
    "skills_hub",
    "approval",
    "mcp",
    "title_generation",
]
# Upstream `/api/model/auxiliary` shape is ``{task, provider, model, base_url}``
# per slot. We keep ``api_key`` as a Hermes-Browser-Extension-only field so the
# plugin can carry per-slot keys in ``<plugin-root>/.env`` — upstream doesn't
# offer an equivalent.
_AUX_SLOT_FIELDS = ("provider", "model", "base_url", "api_key")


def _read_aux_slot(block: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for f in _AUX_SLOT_FIELDS:
        v = block.get(f)
        out[f] = str(v).strip() if v is not None and str(v).strip() else ""
    return out


def read_auxiliary_models() -> Dict[str, Any]:
    """Return auxiliary-slot configuration in upstream-aligned shape.

    Output mirrors upstream ``GET /api/model/auxiliary``::

        {
          "tasks": [
            {"task": "vision", "provider": "auto", "model": "", "base_url": "", "api_key": ""},
            ...
          ],
          "main": {"provider": "openrouter", "model": "anthropic/claude-opus-4.7"},
        }

    The extra ``api_key`` per task is bridge-only (see ``_AUX_SLOT_FIELDS``
    comment); upstream's task block doesn't include it.
    """
    path = _config_yaml_path()
    empty_tasks: List[Dict[str, str]] = [
        {"task": slot, **{f: "" for f in _AUX_SLOT_FIELDS}}
        for slot in AUXILIARY_SLOTS
    ]
    base: Dict[str, Any] = {
        "config_path": str(path),
        "config_exists": path.exists(),
        "tasks": empty_tasks,
        "main": {"provider": "", "model": ""},
    }
    if not path.exists():
        return base
    yaml = _load_yaml_module()
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return {**base, "config_exists": True, "error": str(e)}
    if not isinstance(cfg, dict):
        cfg = {}

    aux_block = cfg.get("auxiliary")
    if not isinstance(aux_block, dict):
        aux_block = {}
    tasks: List[Dict[str, str]] = []
    for slot in AUXILIARY_SLOTS:
        sb = aux_block.get(slot)
        row = (
            _read_aux_slot(sb)
            if isinstance(sb, dict)
            else {f: "" for f in _AUX_SLOT_FIELDS}
        )
        tasks.append({"task": slot, **row})

    # Main slot mirrored for convenience — matches upstream so the UI can
    # render the main model + aux slots side-by-side without a second
    # request.
    model_cfg = cfg.get("model")
    if isinstance(model_cfg, dict):
        main_name = model_cfg.get("default") or model_cfg.get("model") or ""
        main = {
            "provider": str(model_cfg.get("provider", "") or ""),
            "model": str(main_name or ""),
        }
    else:
        main = {
            "provider": "",
            "model": str(model_cfg) if model_cfg else "",
        }

    return {
        "config_path": str(path),
        "config_exists": True,
        "tasks": tasks,
        "main": main,
    }


def write_auxiliary_slot(
    task: str,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Write one auxiliary task slot. Param name ``task`` matches upstream."""
    task = task.strip()
    if task not in AUXILIARY_SLOTS:
        raise ValueError(f"unknown auxiliary task: {task!r}. Valid: {AUXILIARY_SLOTS}")
    path = _config_yaml_path()
    yaml = _load_yaml_module()
    cfg: Dict[str, Any] = {}
    if path.exists():
        try:
            cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            raise ValueError(f"cannot parse existing config.yaml: {e}") from e
    if not isinstance(cfg, dict):
        cfg = {}

    aux_block = cfg.get("auxiliary")
    if not isinstance(aux_block, dict):
        aux_block = {}
    aux_block = dict(aux_block)
    slot_block = aux_block.get(task)
    if not isinstance(slot_block, dict):
        slot_block = {}
    slot_block = dict(slot_block)

    def _set_str(d: dict, key: str, val: Optional[str]) -> None:
        if val is not None:
            d[key] = str(val).strip()

    _set_str(slot_block, "provider", provider)
    _set_str(slot_block, "model", model)
    _set_str(slot_block, "base_url", base_url)
    _set_str(slot_block, "api_key", api_key)

    aux_block[task] = slot_block
    cfg["auxiliary"] = aux_block
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False)
    path.write_text(text, encoding="utf-8")
    return read_auxiliary_models()

