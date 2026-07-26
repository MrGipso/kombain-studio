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
from core.comfy import validate_configured_workflows

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
    if settings.lm_model and settings.lm_model != "local-model":
        if settings.lm_model not in model_ids:
            return CheckResult(
                target="lm", ok=False,
                message=f"LM Studio доступен, но модель '{settings.lm_model}' не загружена",
                detail=", ".join(model_ids[:10]),
            )
    return CheckResult(target="lm", ok=True, message=msg, detail=", ".join(model_ids[:10]))


def _check_lm_inference(settings) -> CheckResult:
    """Explicit active probe; may JIT-load the configured chat model."""
    availability = _check_lm(settings)
    if not availability.ok:
        return availability
    base = (settings.lm_url or "http://127.0.0.1:1234/v1").rstrip("/")
    model = str(settings.lm_model or "").strip()
    if not model or model == "local-model":
        return CheckResult(
            target="lm", ok=False, message="LM model не настроена", detail=model,
        )
    try:
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "Reply with OK"}],
            "temperature": 0,
            "max_tokens": 8,
            "ttl": 5,
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{base}/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        content = str(
            (((data.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
        ).strip()
        if content:
            return CheckResult(
                target="lm", ok=True, message="LM inference работает", detail=model,
            )
        return CheckResult(
            target="lm", ok=False, message="LM вернула пустой ответ", detail=model,
        )
    except Exception as exc:
        return CheckResult(
            target="lm", ok=False, message="LM inference недоступен", detail=str(exc),
        )


def _check_comfy(settings) -> CheckResult:
    base = (settings.comfy_url or "http://127.0.0.1:8188").rstrip("/")
    ok, body = _http_get(f"{base}/system_stats")
    if not ok:
        ok, body = _http_get(f"{base}/")
    if ok:
        try:
            report = validate_configured_workflows(settings.comfy_url, settings)
        except Exception as exc:
            return CheckResult(
                target="comfy",
                ok=False,
                message="ComfyUI доступен, но workflows не удалось проверить",
                detail=str(exc),
            )
        problems = []
        for name, item in report["workflows"].items():
            if item.get("ok"):
                continue
            missing = item.get("missing_node_types") or []
            problems.append(
                f"{name}: {', '.join(missing) if missing else item.get('error', 'invalid workflow')}"
            )
        if problems:
            return CheckResult(
                target="comfy",
                ok=False,
                message="ComfyUI доступен, но workflows несовместимы",
                detail="; ".join(problems),
            )
        return CheckResult(
            target="comfy",
            ok=True,
            message="ComfyUI и workflows готовы",
            detail=(
                f"{len(report['workflows'])} workflows, "
                f"{report['available_node_types']} node types"
            ),
        )
    return CheckResult(target="comfy", ok=False, message="ComfyUI недоступен", detail=body)


def _check_binary(target: str, configured: str, default_name: str) -> CheckResult:
    candidate = configured or ""
    allowed_names = {default_name.lower(), f"{default_name.lower()}.exe"}
    path = Path(candidate) if candidate else None
    if path and path.exists():
        if path.name.lower() not in allowed_names:
            return CheckResult(
                target=target,
                ok=False,
                message=f"{default_name} не запущен",
                detail=f"Ожидался бинарник {default_name}, указан: {path.name}",
            )
        resolved = str(path)
    else:
        found = shutil.which(candidate) or shutil.which(default_name)
        if not found:
            return CheckResult(
                target=target, ok=False,
                message=f"{default_name} не найден",
                detail=f"Указанный путь: {candidate or '(пусто)'}",
            )
        if Path(found).name.lower() not in allowed_names:
            return CheckResult(
                target=target,
                ok=False,
                message=f"{default_name} не запущен",
                detail=f"Ожидался бинарник {default_name}, найден: {Path(found).name}",
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


def _check_omnivoice(settings) -> CheckResult:
    presence = _check_binary("omnivoice", settings.omnivoice_cli, "omnivoice-infer")
    if not presence.ok:
        return presence
    configured = settings.omnivoice_cli or "omnivoice-infer"
    resolved = shutil.which(configured) or (configured if Path(configured).is_file() else "")
    if not resolved:
        return presence
    try:
        probe = subprocess.run(
            [resolved, "--help"], capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
        detail = (probe.stdout or probe.stderr or "").strip()
        if probe.returncode == 0:
            return CheckResult(
                target="omnivoice", ok=True,
                message="OmniVoice готов к запуску",
                detail=str(resolved),
            )
        if "audioop" in detail.lower() or "pyaudioop" in detail.lower():
            return CheckResult(
                target="omnivoice", ok=False,
                message="OmniVoice несовместим с текущим Python 3.13",
                detail="Установите audioop-lts в окружение OmniVoice.",
            )
        return CheckResult(
            target="omnivoice", ok=False,
            message="OmniVoice найден, но не запускается",
            detail=detail[-1000:],
        )
    except Exception as exc:
        return CheckResult(
            target="omnivoice", ok=False,
            message="Не удалось проверить запуск OmniVoice",
            detail=str(exc),
        )


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


def _check_embed_available(settings) -> CheckResult:
    """Passive model-list probe that never submits an embedding request."""
    base = (settings.lm_url or "http://127.0.0.1:1234/v1").rstrip("/")
    model = getattr(settings, "lm_embed_model", "") or "text-embedding-nomic-embed-text-v1.5"
    ok, body = _http_get(f"{base}/models")
    if not ok:
        return CheckResult(
            target="embed", ok=False, message="LM Studio недоступен", detail=body,
        )
    try:
        data = json.loads(body)
        model_ids = [str(item.get("id", "")) for item in data.get("data", [])]
    except (json.JSONDecodeError, TypeError, AttributeError):
        return CheckResult(
            target="embed", ok=False, message="Некорректный список моделей LM Studio",
            detail=body[:500],
        )
    if model not in model_ids:
        return CheckResult(
            target="embed", ok=False,
            message=f"Embedding model '{model}' не загружена",
            detail=", ".join(model_ids[:10]),
        )
    return CheckResult(
        target="embed", ok=True, message="Embedding model доступна", detail=model,
    )


def _check_woosh(settings) -> CheckResult:
    if not getattr(settings, "sfx_enabled", False):
        return CheckResult(target="woosh", ok=True, message="SFX-слой выключен", detail="Включите sfx_enabled в настройках")
    base = (getattr(settings, "woosh_url", "") or "").rstrip("/")
    if not base:
        return CheckResult(target="woosh", ok=False, message="Woosh URL не задан")
    ok, body = _http_get(f"{base}/ping")
    if ok:
        return CheckResult(target="woosh", ok=True, message="Woosh доступен", detail=base)
    return CheckResult(target="woosh", ok=False, message="Woosh недоступен", detail=body)


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
    if target == "omnivoice":
        return _check_omnivoice(settings)
    if target == "woosh":
        return _check_woosh(settings)
    return CheckResult(target=target, ok=False, message=f"Неизвестная проверка: {target}")
