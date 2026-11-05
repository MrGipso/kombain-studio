"""LM Studio / cloud OpenAI-compatible client — the single home for chat calls.

Every caller that needs "POST /chat/completions, add TTL, strip markdown
fences, dig JSON out of the reply" imports this module instead of growing its
own copy.

This module must stay dependency-light: no imports from ``backend``.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from email.utils import parsedate_to_datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import TypeAdapter, ValidationError

DEFAULT_URL = "http://127.0.0.1:1234/v1"
JSON_SYSTEM = "Отвечай только валидным JSON. Не добавляй markdown."

# ``lm_provider`` values. LM Studio is the local default; any other
# OpenAI-compatible server (OpenAI, OpenRouter, Gemini, a LiteLLM proxy …)
# gets the API key but none of LM Studio's extensions (TTL, load/unload, SDK).
PROVIDER_LMSTUDIO = "lmstudio"
PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"

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


def _setting(settings: Any, name: str, default: Any = None) -> Any:
    """Read one field from a Settings object or a plain settings mapping."""
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def _normalize_provider(value: Any) -> str:
    if str(value or "").strip().lower() == PROVIDER_OPENAI_COMPATIBLE:
        return PROVIDER_OPENAI_COMPATIBLE
    return PROVIDER_LMSTUDIO


def _origin(url: Any) -> str:
    """``scheme://host:port`` of an http(s) URL, or "" when it has none."""
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
        port = parts.port
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        return ""
    return f"{scheme}://{host}:{port or (443 if scheme == 'https' else 80)}"


@dataclass(frozen=True)
class _Endpoint:
    origin: str
    provider: str
    api_key: str
    daily_limit: int = 0  # cloud requests per local day; 0 = unlimited


def _endpoint_from(settings: Any) -> _Endpoint | None:
    origin = _origin(base_url(settings))
    if not origin:
        return None
    provider = _normalize_provider(_setting(settings, "lm_provider"))
    # The key is only ever attached in cloud mode: switching back to a local
    # LM Studio must not keep sending a stored cloud key to another server.
    api_key = ""
    if provider == PROVIDER_OPENAI_COMPATIBLE:
        api_key = str(_setting(settings, "lm_api_key", "") or "").strip()
    try:
        daily_limit = max(0, int(_setting(settings, "lm_cloud_daily_limit", 0) or 0))
    except (TypeError, ValueError):
        daily_limit = 0
    return _Endpoint(origin=origin, provider=provider, api_key=api_key, daily_limit=daily_limit)


# The configured LM endpoint. Many helpers receive only a base URL (scene and
# shot planning, stock scoring, music brief), so credentials are resolved from
# the last loaded settings by origin instead of being threaded through every
# signature. Replaced as a whole, never mutated, so readers need no lock.
_configured: _Endpoint | None = None


def configure_endpoint(settings: Any) -> None:
    """Remember the provider and API key of the configured LM endpoint.

    Called whenever settings are loaded or saved. The key is attached only to
    requests whose origin equals ``lm_url``'s, so it never reaches another host.
    """
    global _configured
    _configured = _endpoint_from(settings)


def _endpoint_for(url: str, settings: Any = None) -> _Endpoint | None:
    if settings is not None and _setting(settings, "lm_provider") is not None:
        endpoint = _endpoint_from(settings)
    else:
        endpoint = _configured
    if endpoint is not None and endpoint.origin == _origin(url):
        return endpoint
    return None


def request_headers(url: str, settings: Any = None) -> dict[str, str]:
    """JSON headers for an LM request, plus Bearer auth for the cloud endpoint."""
    headers = {"Content-Type": "application/json"}
    endpoint = _endpoint_for(url, settings)
    if endpoint is not None and endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"
    return headers


def is_lmstudio(settings: Any) -> bool:
    """Whether the LM endpoint is LM Studio (TTL, load/unload, SDK available).

    An explicit ``lm_provider`` wins; URL-only settings shims fall back to the
    configured endpoint, and an unknown server keeps the historic LM Studio
    behaviour.
    """
    explicit = _setting(settings, "lm_provider")
    if explicit is not None:
        return _normalize_provider(explicit) == PROVIDER_LMSTUDIO
    endpoint = _endpoint_for(base_url(settings))
    return endpoint is None or endpoint.provider == PROVIDER_LMSTUDIO


class CloudRequestLimitError(RuntimeError):
    """The user's daily cap on cloud LLM requests is reached."""


# Rate limits and transient gateway/overload errors (529 = Anthropic-style
# "overloaded"). Other 4xx are the caller's fault and are never replayed.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 529})
_MAX_ATTEMPTS = 4
_MAX_RETRY_WAIT_SECONDS = 30.0
_sleep = time.sleep
_log = logging.getLogger(__name__)

# In-memory, per local day: reset at midnight and when the server restarts.
_usage_lock = threading.Lock()
_usage_day: date | None = None
_usage_count = 0


def cloud_requests_today() -> int:
    """Cloud LLM requests sent today by this server process."""
    with _usage_lock:
        return _usage_count if _usage_day == date.today() else 0


def _consume_cloud_request(limit: int) -> None:
    global _usage_day, _usage_count
    with _usage_lock:
        today = date.today()
        if _usage_day != today:
            _usage_day, _usage_count = today, 0
        if limit > 0 and _usage_count >= limit:
            raise CloudRequestLimitError(
                f"Достигнут суточный лимит облачных LLM-запросов ({limit}). "
                "Увеличьте лимит в настройках или продолжите завтра."
            )
        _usage_count += 1


def _retry_delay(exc: urllib.error.HTTPError, attempt: int) -> float:
    """Seconds to wait before retry ``attempt``: Retry-After, else 2/4/8 s."""
    raw = exc.headers.get("Retry-After") if exc.headers is not None else None
    delay: float | None = None
    if raw:
        text = str(raw).strip()
        try:
            delay = float(text)
        except ValueError:
            try:
                delay = parsedate_to_datetime(text).timestamp() - time.time()
            except (TypeError, ValueError, IndexError, OverflowError):
                delay = None
    if delay is None:
        delay = 2.0 ** attempt
    return min(max(delay, 0.5), _MAX_RETRY_WAIT_SECONDS)


def post_json(url: str, payload: Any, *, timeout: float, settings: Any = None) -> Any:
    """POST JSON to an LM endpoint and return the decoded JSON reply.

    Every LLM request goes through here. Cloud endpoints count against the
    daily cap (one count per call, however many retries) and replay rate
    limits and transient server errors, honouring Retry-After. LM Studio keeps
    a single attempt: its 5xx are usually deterministic (context too long).
    """
    endpoint = _endpoint_for(url, settings)
    cloud = endpoint is not None and endpoint.provider == PROVIDER_OPENAI_COMPATIBLE
    if cloud:
        _consume_cloud_request(endpoint.daily_limit)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = request_headers(url, settings)
    attempts = _MAX_ATTEMPTS if cloud else 1
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            if attempt == attempts or exc.code not in _RETRY_STATUSES:
                raise
            delay = _retry_delay(exc, attempt)
            exc.close()
            _log.warning(
                "LLM %s: HTTP %s, повтор %s/%s через %.1f с",
                url, exc.code, attempt, attempts - 1, delay,
            )
            _sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def unload_delay_seconds(settings: Any) -> int | None:
    """The user's LM Studio auto-unload policy for the end of a stage.

    None = auto-unload disabled; 0 = unload explicitly right after the stage;
    positive int = idle seconds before LM Studio unloads the model. Accepts a
    Settings object or a plain settings mapping; an unparsable delay is 60 s.
    A cloud provider manages its own models, so the policy is always None.
    """
    if not is_lmstudio(settings):
        return None
    auto = _setting(settings, "lm_auto_unload", True)
    if isinstance(auto, str):
        auto = auto.strip().lower() not in {"0", "false", "no", "off"}
    if not auto:
        return None
    try:
        return max(0, int(str(_setting(settings, "lm_unload_delay", "0")).strip() or "0"))
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
    return (str(_setting(settings, "lm_url", "") or "") or DEFAULT_URL).strip().rstrip("/")


def model_name(settings: Any) -> str:
    return (str(_setting(settings, "lm_model", "") or "") or "local-model").strip() or "local-model"


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
    lmstudio = is_lmstudio(settings)
    if lmstudio:
        add_ttl(payload, ttl if ttl is not None else ttl_seconds(settings))
    url = base_url(settings) + "/chat/completions"
    if response_schema is None:
        data = post_json(url, payload, timeout=timeout, settings=settings)
    elif lmstudio:
        payload["response_format"] = _response_format(
            "json_schema", schema_name, response_schema, strict=True
        )
        data = post_json(url, payload, timeout=timeout, settings=settings)
    else:
        data = _post_cloud_structured(
            url, payload, response_schema, schema_name, timeout=timeout, settings=settings
        )
    return _assistant_content(data)


# Structured-output modes, strongest first. Providers differ: OpenAI and
# Gemini accept json_schema, DeepSeek only json_object, some proxies neither.
# The reply is validated locally in every mode, so a weaker mode only costs
# reliability, never correctness.
_FORMAT_MODES = ("json_schema", "json_object", "prompt")
# (origin, model) -> the strongest mode the provider accepted.
_format_modes: dict[tuple[str, str], str] = {}
_FORMAT_REJECTION_MARKERS = ("response_format", "json_schema", "json_object", "structured output")


def _response_format(mode: str, name: str, schema: dict[str, Any], *, strict: bool) -> dict | None:
    if mode == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {"name": name, "strict": strict, "schema": schema},
        }
    if mode == "json_object":
        return {"type": "json_object"}
    return None


def _with_schema_hint(messages: list[dict], schema: dict[str, Any]) -> list[dict]:
    """Put the schema into the system prompt when the API can't enforce it."""
    hint = (
        "Ответ — только JSON без markdown, строго по этой JSON Schema:\n"
        + json.dumps(schema, ensure_ascii=False)
    )
    copied = [dict(message) for message in messages]
    first = copied[0] if copied else None
    if first is not None and first.get("role") == "system" and isinstance(first.get("content"), str):
        first["content"] = first["content"].rstrip() + "\n\n" + hint
    else:
        copied.insert(0, {"role": "system", "content": hint})
    return copied


def _post_cloud_structured(
    url: str,
    payload: dict[str, Any],
    schema: dict[str, Any],
    schema_name: str,
    *,
    timeout: float,
    settings: Any,
) -> Any:
    """POST a structured request, stepping down modes the provider rejects."""
    # json_object can only describe an object; array schemas go to the prompt.
    modes = [m for m in _FORMAT_MODES if m != "json_object" or schema.get("type") == "object"]
    key = (_origin(url), str(payload.get("model", "")))
    start = _format_modes.get(key, modes[0])
    modes = modes[modes.index(start):] if start in modes else modes
    for index, mode in enumerate(modes):
        attempt = dict(payload)
        # OpenAI's strict mode rejects schemas with optional fields; replies
        # are validated and repaired locally either way.
        response_format = _response_format(mode, schema_name, schema, strict=False)
        if response_format is not None:
            attempt["response_format"] = response_format
        if mode != "json_schema":
            attempt["messages"] = _with_schema_hint(payload["messages"], schema)
        try:
            data = post_json(url, attempt, timeout=timeout, settings=settings)
        except urllib.error.HTTPError as exc:
            if index == len(modes) - 1 or exc.code not in {400, 422}:
                raise
            body = exc.read().decode("utf-8", errors="replace")
            if not any(marker in body.lower() for marker in _FORMAT_REJECTION_MARKERS):
                # Not about response_format: surface it with the body intact.
                raise urllib.error.HTTPError(
                    exc.url, exc.code, exc.msg, exc.headers, io.BytesIO(body.encode("utf-8"))
                ) from None
            _log.warning(
                "LLM %s отклонил response_format=%s, пробую %s", url, mode, modes[index + 1]
            )
            continue
        _format_modes[key] = mode
        return data
    raise AssertionError("unreachable")  # pragma: no cover


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
    if is_lmstudio(settings):
        add_ttl(payload, ttl if ttl is not None else ttl_seconds(settings))
    data = post_json(
        base_url(settings) + "/chat/completions", payload, timeout=timeout, settings=settings
    )
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
    if not is_lmstudio(settings):
        if requested == "sdk":
            raise ValueError("LM Studio SDK недоступен для облачного API; используйте transport='openai'")
        return "openai", None, None
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
        if exc.code in {401, 403}:
            return (
                f"Сервер LLM отклонил запрос (HTTP {exc.code}): проверьте API-ключ "
                "и доступ к модели.\n" + message
            )
        if exc.code == 429:
            return (
                "Провайдер LLM ограничил частоту запросов или исчерпан баланс "
                "(HTTP 429). Подождите и повторите.\n" + message
            )
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
    url = rest_base(openai_base_url) + "/models/load"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=request_headers(url),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.loads(response.read().decode("utf-8"))


def unload_model(openai_base_url: str, instance_id: str) -> dict:
    url = rest_base(openai_base_url) + "/models/unload"
    request = urllib.request.Request(
        url,
        data=json.dumps({"instance_id": str(instance_id).strip()}).encode("utf-8"),
        headers=request_headers(url),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))
