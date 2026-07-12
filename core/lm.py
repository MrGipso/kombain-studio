"""LM Studio (OpenAI-compatible) client — the single home for chat calls.

First strangler slice out of content_pipeline_gui.py: the monolith, the script
router and the assets router each grew their own copy of "POST /chat/completions,
add TTL, strip markdown fences, dig JSON out of the reply". New code imports
this module; the monolith's copies migrate over incrementally so the web
backend stays independent of the desktop application.

No imports from the monolith — this module must stay dependency-light.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

DEFAULT_URL = "http://127.0.0.1:1234/v1"
JSON_SYSTEM = "Отвечай только валидным JSON. Не добавляй markdown."


def ttl_seconds(settings: Any) -> int | None:
    """LM Studio JIT auto-unload TTL derived from the user's settings.

    None = do not send a TTL (auto-unload disabled); positive int = seconds.
    A configured delay of 0 means "unload immediately after the stage", which
    callers implement via an explicit unload — not via TTL — so 0 maps to None.
    """
    auto = getattr(settings, "lm_auto_unload", True)
    if isinstance(auto, str):
        auto = auto.strip().lower() not in {"0", "false", "no", "off"}
    if not auto:
        return None
    try:
        delay = max(0, int(str(getattr(settings, "lm_unload_delay", "0")).strip() or "0"))
    except ValueError:
        delay = 60
    return delay if delay > 0 else None


def add_ttl(payload: dict, ttl: int | None) -> None:
    if ttl is not None and ttl > 0:
        payload["ttl"] = int(ttl)


def base_url(settings: Any) -> str:
    return (getattr(settings, "lm_url", "") or DEFAULT_URL).rstrip("/")


def model_name(settings: Any) -> str:
    return (getattr(settings, "lm_model", "") or "local-model").strip() or "local-model"


def strip_fences(content: str) -> str:
    """Remove a ```json ... ``` fence if the model added one despite the rules."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def extract_json(content: str) -> Any:
    """Parse the first JSON object or array found in a model reply."""
    text = strip_fences(content)
    start_obj, start_arr = text.find("{"), text.find("[")
    if start_arr >= 0 and (start_obj < 0 or start_arr < start_obj):
        end = text.rfind("]")
        if end > start_arr:
            return json.loads(text[start_arr:end + 1])
    if start_obj >= 0:
        end = text.rfind("}")
        if end > start_obj:
            return json.loads(text[start_obj:end + 1])
    raise ValueError("Модель вернула не-JSON ответ")


def chat(settings: Any, messages: list[dict], temperature: float = 0.5,
         timeout: int = 300, ttl: int | None = None) -> str:
    """One chat-completions call; returns the raw assistant text."""
    payload: dict[str, Any] = {
        "model": model_name(settings),
        "messages": messages,
        "temperature": temperature,
    }
    add_ttl(payload, ttl if ttl is not None else ttl_seconds(settings))
    request = urllib.request.Request(
        base_url(settings) + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def chat_json(settings: Any, prompt: str, system: str = JSON_SYSTEM,
              temperature: float = 0.5, timeout: int = 300) -> Any:
    """Chat call that must return JSON (object or array)."""
    content = chat(
        settings,
        [{"role": "system", "content": system},
         {"role": "user", "content": prompt}],
        temperature=temperature,
        timeout=timeout,
    )
    return extract_json(content)


def explain_error(exc: Exception) -> str:
    """Turn common LM Studio transport/context failures into actionable text."""
    message = str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", errors="replace")
            message = f"{message}\n{body}"
        except Exception:
            pass
    lower = message.lower()
    if "exceeds the available context size" in lower or (
        "context" in lower and "exceeds" in lower
    ):
        return (
            "LM Studio отклонил запрос: данные не помещаются в context length. "
            "Увеличьте контекст модели минимум до 32768 и повторите."
        )
    if (
        "connection refused" in lower
        or "actively refused" in lower
        or "urlopen error" in lower
    ):
        return "Не удалось подключиться к LM Studio. Проверьте сервер и URL."
    return message


def rest_base(openai_base_url: str) -> str:
    """Translate an OpenAI-compatible `/v1` URL to LM Studio's REST base."""
    value = str(openai_base_url or DEFAULT_URL).strip().rstrip("/")
    if value.endswith("/api/v1"):
        return value
    if value.endswith("/v1"):
        value = value[:-3]
    if value.endswith("/api"):
        value = value[:-4]
    return value + "/api/v1"


def load_model(openai_base_url: str, model: str, context_length: int) -> dict:
    payload = {
        "model": str(model).strip(),
        "context_length": int(context_length),
        "flash_attention": True,
        "echo_load_config": True,
    }
    request = urllib.request.Request(
        rest_base(openai_base_url) + "/models/load",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.loads(response.read().decode("utf-8"))


def unload_model(openai_base_url: str, instance_id: str) -> dict:
    request = urllib.request.Request(
        rest_base(openai_base_url) + "/models/unload",
        data=json.dumps({"instance_id": str(instance_id).strip()}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))
