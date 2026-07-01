from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from core.config import load_settings

router = APIRouter()


class CheckResult(BaseModel):
    target: str
    ok: bool
    message: str
    detail: str = ""


def _http_get(url: str, timeout: int = 5) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(4000).decode("utf-8", errors="replace")
            return True, body
    except urllib.error.HTTPError as exc:
        # An HTTP error still means the server is reachable.
        return True, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)


def _check_lm(settings) -> CheckResult:
    base = (settings.lm_url or "http://127.0.0.1:1234/v1").rstrip("/")
    ok, body = _http_get(f"{base}/models")
    if not ok:
        return CheckResult(target="lm", ok=False, message="LM Studio недоступен", detail=body)
    model_ids = []
    try:
        data = json.loads(body)
        model_ids = [m.get("id", "") for m in data.get("data", [])]
    except Exception:
        pass
    msg = "LM Studio доступен"
    if settings.lm_model and settings.lm_model != "local-model" and model_ids:
        if settings.lm_model not in model_ids:
            return CheckResult(
                target="lm", ok=True,
                message=f"LM Studio доступен, но модель '{settings.lm_model}' не в списке",
                detail=", ".join(model_ids[:10]),
            )
    return CheckResult(target="lm", ok=True, message=msg, detail=", ".join(model_ids[:10]))


def _check_comfy(settings) -> CheckResult:
    base = (settings.comfy_url or "http://127.0.0.1:8188").rstrip("/")
    ok, body = _http_get(f"{base}/system_stats")
    if not ok:
        ok, body = _http_get(f"{base}/")
    if ok:
        return CheckResult(target="comfy", ok=True, message="ComfyUI доступен")
    return CheckResult(target="comfy", ok=False, message="ComfyUI недоступен", detail=body)


def _check_binary(target: str, configured: str, default_name: str) -> CheckResult:
    candidate = configured or ""
    path = Path(candidate) if candidate else None
    if path and path.exists():
        resolved = str(path)
    else:
        found = shutil.which(candidate) or shutil.which(default_name)
        if not found:
            return CheckResult(
                target=target, ok=False,
                message=f"{default_name} не найден",
                detail=f"Указанный путь: {candidate or '(пусто)'}",
            )
        resolved = found
    try:
        out = subprocess.run(
            [resolved, "--version"], capture_output=True, text=True, timeout=8,
        )
        first = (out.stdout or out.stderr or "").splitlines()[:1]
        return CheckResult(target=target, ok=True, message=f"{default_name} найден", detail=(first[0] if first else resolved))
    except Exception as exc:
        # Exists but couldn't run --version; still treat as present.
        return CheckResult(target=target, ok=True, message=f"{default_name} найден", detail=f"{resolved} ({exc})")


def _check_embed(settings) -> CheckResult:
    base = (settings.lm_url or "http://127.0.0.1:1234/v1").rstrip("/")
    model = getattr(settings, "lm_embed_model", "") or "text-embedding-nomic-embed-text-v1.5"
    try:
        # ttl makes LM Studio auto-unload the model shortly after this probe, so
        # a connection test doesn't leave the embedding model resident for 60 min.
        payload = json.dumps({"model": model, "input": ["test"], "ttl": 5}).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/embeddings", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        dim = len(((data.get("data") or [{}])[0]).get("embedding", []))
        if dim:
            return CheckResult(target="embed", ok=True, message="Эмбеддинги работают", detail=f"{model}, dim={dim}")
        return CheckResult(target="embed", ok=False, message="Пустой ответ эмбеддингов", detail=model)
    except urllib.error.HTTPError as exc:
        return CheckResult(target="embed", ok=False, message=f"Модель эмбеддингов недоступна (HTTP {exc.code})", detail=f"Загрузи '{model}' в LM Studio")
    except Exception as exc:
        return CheckResult(target="embed", ok=False, message="Эмбеддинги недоступны", detail=str(exc))


def _check_whisper(settings) -> CheckResult:
    cli = settings.whisper_cli or ""
    model = settings.whisper_model or ""
    cli_ok = bool(cli) and Path(cli).exists()
    model_ok = bool(model) and Path(model).exists()
    if cli_ok and model_ok:
        return CheckResult(target="whisper", ok=True, message="Whisper CLI и модель найдены", detail=Path(model).name)
    missing = []
    if not cli_ok:
        missing.append(f"CLI ({cli or 'не указан'})")
    if not model_ok:
        missing.append(f"модель ({model or 'не указана'})")
    return CheckResult(target="whisper", ok=False, message="Whisper не настроен", detail="Нет: " + ", ".join(missing))


@router.get("/diagnostics/{target}")
async def run_check(target: str):
    settings = load_settings()
    if target == "lm":
        return _check_lm(settings)
    if target == "embed":
        return _check_embed(settings)
    if target == "comfy":
        return _check_comfy(settings)
    if target == "ffmpeg":
        return _check_binary("ffmpeg", settings.ffmpeg, "ffmpeg")
    if target == "whisper":
        return _check_whisper(settings)
    return CheckResult(target=target, ok=False, message=f"Неизвестная проверка: {target}")
