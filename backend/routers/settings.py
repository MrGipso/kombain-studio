from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter

from core.config import load_settings, save_settings, list_presets
from backend.models.schemas import Settings

router = APIRouter()


@router.get("/settings")
async def get_settings():
    s = load_settings()
    return {f.name: getattr(s, f.name) for f in s.__dataclass_fields__.values()}


@router.put("/settings")
async def update_settings(data: dict[str, Any]):
    s = load_settings()
    valid_keys = {f.name for f in s.__dataclass_fields__.values()}
    for key, value in data.items():
        if key in valid_keys:
            setattr(s, key, value)
    save_settings(s)
    return {"ok": True}


@router.get("/presets")
async def get_presets():
    presets = list_presets()
    return [
        {
            "name": p.name,
            "path": str(p.path),
            "info": p.raw.get("info", {}),
            "routes": p.routes,
        }
        for p in presets
    ]
