"""LM Studio (OpenAI-compatible) client — the single home for chat calls.

Every caller that needs "POST /chat/completions, add TTL, strip markdown
fences, dig JSON out of the reply" imports this module instead of growing its
own copy.

This module must stay dependency-light: no imports from ``backend``.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

from pydantic import TypeAdapter, ValidationError

DEFAULT_URL = "http://127.0.0.1:1234/v1"
JSON_SYSTEM = "Отвечай только валидным JSON. Не добавляй markdown."

T = TypeVar("T")
Transport = Literal["auto", "sdk", "openai"]


@dataclass(frozen=True)
class StructuredReply(Generic[T]):
    """Validated structured value plus small, non-sensitive request metadata."""

    value: T
    attempts: int
    transport: Literal["sdk", "openai"]


@dataclass(frozen=True)
class ChatReply:
    """Plain assistant text plus completion diagnostics from LM Studio."""

    content: str
    finish_reason: str
    prompt_tokens: int | None
    completion_tokens: int | None


class StructuredOutputError(ValueError):
    """The model exhausted its single schema-repair attempt."""


class AssistantContentError(ValueError):
    """The server returned a completion envelope without usable text."""


def unload_delay_seconds(settings: Any) -> int | None:
    """The user's LM Studio auto-unload policy for the end of a stage.

    None = auto-unload disabled; 0 = unload explicitly right after the stage;
    positive int = idle seconds before LM Studio unloads the model. Accepts a
    Settings object or a plain settings mapping; an unparsable delay is 60 s.
    """
    def read(name: str, default: Any) -> Any:
        if isinstance(settings, Mapping):
            return settings.get(name, default)
        return getattr(settings, name, default)

    auto = read("lm_auto_unload", True)
    if isinstance(auto, str):
        auto = auto.strip().lower() not in {"0", "false", "no", "off"}
    if not auto:
        return None
    try:
        return max(0, int(str(read("lm_unload_delay", "0")).strip() or "0"))
    except (TypeError, ValueError):
        return 60


def ttl_seconds(settings: Any) -> int | None:
    """LM Studio JIT auto-unload TTL derived from the user's settings.

    None = do not send a TTL (auto-unload disabled); positive int = seconds.
    A configured delay of 0 means "unload immediately after the stage", which
    callers implement via an explicit unload — not via TTL — so 0 maps to None.
    """
    delay = unload_delay_seconds(settings)
    return delay if delay else None


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


def _assistant_content(data: dict[str, Any]) -> str:
    """Read assistant text without leaking protocol-shaped KeyError/TypeError."""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AssistantContentError(
            "LM Studio вернул ответ без assistant message"
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise AssistantContentError("LM Studio вернул пустой assistant message")
    return content


def _http_chat(
    settings: Any,
    messages: list[dict],
    *,
    temperature: float,
    timeout: int,
    ttl: int | None,
    model: str | None = None,
    max_tokens: int | None = None,
    response_schema: dict[str, Any] | None = None,
    schema_name: str = "structured_response",
) -> str:
    payload: dict[str, Any] = {
        "model": (model or model_name(settings)).strip() or "local-model",
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens is not None:
        if int(max_tokens) <= 0:
            raise ValueError("max_tokens должен быть положительным")
        payload["max_tokens"] = int(max_tokens)
    if response_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": response_schema,
            },
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
    return _assistant_content(data)


def chat(settings: Any, messages: list[dict], temperature: float = 0.5,
         timeout: int = 300, ttl: int | None = None,
         max_tokens: int | None = None) -> str:
    """One chat-completions call; returns the raw assistant text."""
    return _http_chat(
        settings,
        messages,
        temperature=temperature,
        timeout=timeout,
        ttl=ttl,
        max_tokens=max_tokens,
    )


def chat_with_metadata(
    settings: Any,
    messages: list[dict],
    temperature: float = 0.5,
    timeout: int = 300,
    ttl: int | None = None,
    max_tokens: int | None = None,
) -> ChatReply:
    """One plain completion while retaining finish reason and token usage."""
    payload: dict[str, Any] = {
        "model": model_name(settings).strip() or "local-model",
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens is not None:
        if int(max_tokens) <= 0:
            raise ValueError("max_tokens должен быть положительным")
        payload["max_tokens"] = int(max_tokens)
    add_ttl(payload, ttl if ttl is not None else ttl_seconds(settings))
    request = urllib.request.Request(
        base_url(settings) + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    content = _assistant_content(data)
    choice = data.get("choices", [{}])[0] if isinstance(data.get("choices"), list) else {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return ChatReply(
        content=content,
        finish_reason=str(choice.get("finish_reason") or "unknown"),
        prompt_tokens=int(usage["prompt_tokens"]) if isinstance(usage.get("prompt_tokens"), int) else None,
        completion_tokens=(
            int(usage["completion_tokens"]) if isinstance(usage.get("completion_tokens"), int) else None
        ),
    )


def _load_lmstudio() -> Any | None:
    """Lazy optional import so the regular web install stays dependency-light."""
    try:
        import lmstudio  # type: ignore[import-not-found]
    except ImportError:
        return None
    return lmstudio


def _sdk_host(openai_base_url: str) -> str | None:
    """Convert a conventional LM Studio OpenAI URL to the SDK's host:port."""
    parsed = urllib.parse.urlsplit(str(openai_base_url or DEFAULT_URL).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.path.rstrip("/") not in {"", "/v1", "/api/v1"}:
        return None
    return parsed.netloc


def _contains_image(messages: list[dict]) -> bool:
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and (
                item.get("type") in {"image", "image_url"} or "image_url" in item
            ):
                return True
    return False


def _messages_with_image(
    messages: list[dict], image_bytes: bytes | None, image_mime: str
) -> list[dict]:
    copied = [dict(message) for message in messages]
    if image_bytes is None:
        return copied
    if not isinstance(image_bytes, bytes) or not image_bytes:
        raise ValueError("image_bytes должен содержать непустые bytes")
    mime = str(image_mime or "image/jpeg").strip().lower()
    if not re.fullmatch(r"image/(?:jpeg|png|webp)", mime):
        raise ValueError("LM Studio поддерживает JPEG, PNG или WebP")
    image_part = {
        "type": "image_url",
        "image_url": {
            "url": f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        },
    }
    for index in range(len(copied) - 1, -1, -1):
        if copied[index].get("role") != "user":
            continue
        content = copied[index].get("content", "")
        if isinstance(content, str):
            copied[index]["content"] = [
                {"type": "text", "text": content},
                image_part,
            ]
        elif isinstance(content, list):
            copied[index]["content"] = [*content, image_part]
        else:
            raise ValueError("Неподдерживаемый формат user message для изображения")
        return copied
    copied.append({"role": "user", "content": [image_part]})
    return copied


def _schema_name(value: str | None, response_model: Any) -> str:
    raw = value or getattr(response_model, "__name__", "structured_response")
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(raw)).strip("_-")
    return (cleaned or "structured_response")[:64]


def _select_structured_transport(
    settings: Any, requested: Transport, messages: list[dict]
) -> tuple[Literal["sdk", "openai"], Any | None, str | None]:
    if requested not in {"auto", "sdk", "openai"}:
        raise ValueError(f"Неизвестный LM transport: {requested}")
    has_image = _contains_image(messages)
    if requested == "openai" or (requested == "auto" and has_image):
        return "openai", None, None
    if requested == "sdk" and has_image:
        raise ValueError(
            "Python SDK image handles требуют отдельного адаптера; "
            "используйте transport='openai'"
        )

    sdk = _load_lmstudio()
    host = _sdk_host(base_url(settings))
    if sdk is not None and host is not None:
        try:
            ready = bool(sdk.Client.is_valid_api_host(host))
        except Exception:
            ready = False
        if ready:
            return "sdk", sdk, host
    if requested == "sdk":
        raise RuntimeError("LM Studio Python SDK недоступен или не прошёл preflight")
    return "openai", None, None


def _sdk_structured_prediction(
    sdk: Any,
    host: str,
    model: str,
    messages: list[dict],
    schema: dict[str, Any],
    temperature: float,
    max_tokens: int,
    ttl: int | None,
) -> Any:
    model_options: dict[str, Any] = {}
    if ttl is not None and ttl > 0:
        model_options["ttl"] = int(ttl)
    with sdk.Client(host) as client:
        handle = client.llm.model(model, **model_options)
        result = handle.respond(
            {"messages": messages},
            response_format=schema,
            config={"temperature": temperature, "maxTokens": int(max_tokens)},
        )
    return result.parsed


def _repair_messages(
    messages: list[dict],
    invalid_value: Any,
    validation_error: Exception | None = None,
) -> list[dict]:
    repaired = [dict(message) for message in messages]
    if isinstance(invalid_value, str):
        invalid_text = invalid_value
    else:
        invalid_text = json.dumps(invalid_value, ensure_ascii=False, default=str)
    if invalid_text.strip():
        repaired.append({"role": "assistant", "content": invalid_text})
    detail = str(validation_error or "").strip().replace("\n", " ")
    if len(detail) > 500:
        detail = detail[:497] + "..."
    repaired.append(
        {
            "role": "user",
            "content": (
                "Предыдущий ответ не прошёл JSON Schema validation. "
                + (f"Ошибка: {detail}. " if detail else "")
                + "Исправь значения и верни только полный JSON, строго по заданной схеме."
            ),
        }
    )
    return repaired


def chat_structured(
    settings: Any,
    messages: list[dict],
    response_model: type[T] | TypeAdapter[T],
    *,
    model: str | None = None,
    image_bytes: bytes | None = None,
    image_mime: str = "image/jpeg",
    transport: Transport = "auto",
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout: int = 300,
    ttl: int | None = None,
    schema_name: str | None = None,
) -> StructuredReply[T]:
    """Generate and validate JSON, with exactly one validation-repair retry.

    ``auto`` prefers the optional scoped LM Studio SDK after a successful
    preflight. Missing/unavailable SDK falls back to OpenAI-compatible HTTP.
    Once an SDK prediction starts, its errors are propagated and never replayed
    over HTTP. Image input always uses the HTTP multimodal contract.
    """
    if int(max_tokens) <= 0:
        raise ValueError("max_tokens должен быть положительным")
    adapter: TypeAdapter[T] = (
        response_model
        if isinstance(response_model, TypeAdapter)
        else TypeAdapter(response_model)
    )
    schema = adapter.json_schema()
    active_messages = _messages_with_image(messages, image_bytes, image_mime)

    # Keep selection separate from prediction so only a failed preflight can
    # fall back; an exception after generation begins must not duplicate work.
    selected, sdk, host = _select_structured_transport(
        settings, transport, active_messages
    )
    active_model = (model or model_name(settings)).strip() or "local-model"
    active_ttl = ttl if ttl is not None else ttl_seconds(settings)
    name = _schema_name(schema_name, response_model)
    last_error: Exception | None = None

    for attempt in (1, 2):
        if selected == "sdk":
            # sdk/host cannot be None after a successful SDK preflight.
            assert sdk is not None and host is not None
            candidate = _sdk_structured_prediction(
                sdk,
                host,
                active_model,
                active_messages,
                schema,
                temperature,
                int(max_tokens),
                active_ttl,
            )
        else:
            try:
                candidate = _http_chat(
                    settings,
                    active_messages,
                    temperature=temperature,
                    timeout=timeout,
                    ttl=active_ttl,
                    model=active_model,
                    max_tokens=int(max_tokens),
                    response_schema=schema,
                    schema_name=name,
                )
            except AssistantContentError as exc:
                last_error = exc
                if attempt == 2:
                    break
                active_messages = _repair_messages(active_messages, "", exc)
                continue
        try:
            parsed = extract_json(candidate) if isinstance(candidate, str) else candidate
            value = adapter.validate_python(parsed)
        except (ValueError, ValidationError) as exc:
            last_error = exc
            if attempt == 2:
                break
            active_messages = _repair_messages(active_messages, candidate, exc)
            continue
        return StructuredReply(value=value, attempts=attempt, transport=selected)

    detail = str(last_error or "unknown validation error").strip().replace("\n", " ")
    if len(detail) > 800:
        detail = detail[:797] + "..."
    raise StructuredOutputError(
        "LM Studio дважды вернул ответ, не соответствующий JSON Schema: " + detail
    ) from last_error


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
