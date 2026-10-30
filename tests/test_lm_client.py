"""Tests for core.lm — the shared LM Studio client."""

import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from core import lm


class _ScoreResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0, le=1)
    watermark: bool


def _settings(**kw):
    base = dict(lm_url="http://127.0.0.1:1234/v1", lm_model="test-model",
                lm_auto_unload=True, lm_unload_delay="0")
    base.update(kw)
    return SimpleNamespace(**base)


def test_ttl_disabled_when_auto_unload_off():
    assert lm.ttl_seconds(_settings(lm_auto_unload=False)) is None


def test_ttl_zero_delay_maps_to_none():
    # 0 = "unload immediately via explicit call", not via TTL.
    assert lm.ttl_seconds(_settings(lm_unload_delay="0")) is None


def test_ttl_positive_delay():
    assert lm.ttl_seconds(_settings(lm_unload_delay="120")) == 120


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"lm_auto_unload": False}, None),
        ({"lm_auto_unload": "false"}, None),
        ({"lm_auto_unload": "off"}, None),
        ({"lm_unload_delay": "0"}, 0),
        ({"lm_unload_delay": " 45 "}, 45),
        ({"lm_unload_delay": "-5"}, 0),
        ({"lm_unload_delay": "soon"}, 60),
        ({"lm_unload_delay": None}, 60),
    ],
)
def test_unload_delay_policy(overrides, expected):
    assert lm.unload_delay_seconds(_settings(**overrides)) == expected


def test_unload_delay_reads_plain_settings_mappings():
    # pipeline_orch passes settings as a dict; a string "false" must still
    # disable auto-unload there.
    assert lm.unload_delay_seconds({"lm_auto_unload": "false"}) is None
    assert lm.unload_delay_seconds({"lm_unload_delay": "30"}) == 30
    assert lm.unload_delay_seconds({}) == 0


def test_strip_fences():
    assert lm.strip_fences("```json\n{\"a\": 1}\n```") == '{"a": 1}'
    assert lm.strip_fences('{"a": 1}') == '{"a": 1}'


def test_extract_json_object_with_prose():
    data = lm.extract_json('Вот ответ: {"a": 1, "b": [2, 3]} — готово.')
    assert data == {"a": 1, "b": [2, 3]}


def test_extract_json_array_preferred_when_first():
    data = lm.extract_json('[{"x": 1}] trailing {"noise": true}')
    assert data == [{"x": 1}]


def test_extract_json_raises_on_prose():
    with pytest.raises(ValueError):
        lm.extract_json("никакого json здесь нет")


def test_chat_json_uses_transport(monkeypatch):
    captured = {}

    def fake_chat(settings, messages, temperature=0.5, timeout=300, ttl=None):
        captured["messages"] = messages
        captured["temperature"] = temperature
        return '```json\n{"ok": true}\n```'

    monkeypatch.setattr(lm, "chat", fake_chat)
    result = lm.chat_json(_settings(), "prompt-text", system="sys", temperature=0.3)
    assert result == {"ok": True}
    assert captured["messages"][0] == {"role": "system", "content": "sys"}
    assert captured["messages"][1]["content"] == "prompt-text"
    assert captured["temperature"] == 0.3


def _chat_response(content):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": content}}]}
            ).encode("utf-8")

    return Response()


def test_chat_preserves_legacy_openai_payload(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _chat_response(" raw assistant text ")

    monkeypatch.setattr(lm.urllib.request, "urlopen", fake_urlopen)

    content = lm.chat(
        _settings(),
        [{"role": "user", "content": "hello"}],
        temperature=0.35,
        timeout=17,
        ttl=25,
    )

    payload = json.loads(captured["request"].data)
    assert captured["request"].full_url == (
        "http://127.0.0.1:1234/v1/chat/completions"
    )
    assert captured["timeout"] == 17
    assert payload == {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.35,
        "ttl": 25,
    }
    assert content == " raw assistant text "


def test_chat_with_metadata_keeps_finish_reason_and_usage(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "готово"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 45},
            }).encode("utf-8")

    monkeypatch.setattr(lm.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    reply = lm.chat_with_metadata(
        _settings(),
        [{"role": "user", "content": "hello"}],
        max_tokens=200,
    )

    assert reply.content == "готово"
    assert reply.finish_reason == "stop"
    assert reply.prompt_tokens == 120
    assert reply.completion_tokens == 45


def test_chat_structured_http_sends_schema_and_repairs_once(monkeypatch):
    requests = []
    contents = iter(
        [
            '{"score": 9.2, "watermark": false}',
            '{"score": 0.82, "watermark": false}',
        ]
    )

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return _chat_response(next(contents))

    monkeypatch.setattr(lm.urllib.request, "urlopen", fake_urlopen)
    messages = [{"role": "user", "content": "Inspect the frame"}]

    reply = lm.chat_structured(
        _settings(),
        messages,
        _ScoreResponse,
        model="vision-model",
        transport="openai",
        temperature=0.1,
        max_tokens=96,
        timeout=45,
        ttl=20,
        schema_name="frame_qa",
    )

    assert reply.value == _ScoreResponse(score=0.82, watermark=False)
    assert reply.attempts == 2
    assert reply.transport == "openai"
    assert len(requests) == 2
    first = json.loads(requests[0][0].data)
    second = json.loads(requests[1][0].data)
    assert requests[0][1] == requests[1][1] == 45
    assert first["model"] == "vision-model"
    assert first["max_tokens"] == 96
    assert first["ttl"] == 20
    assert first["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "frame_qa",
            "strict": True,
            "schema": TypeAdapter(_ScoreResponse).json_schema(),
        },
    }
    assert second["messages"][-2] == {
        "role": "assistant",
        "content": '{"score": 9.2, "watermark": false}',
    }
    assert "не прошёл JSON Schema" in second["messages"][-1]["content"]
    assert messages == [{"role": "user", "content": "Inspect the frame"}]


def test_chat_structured_exhausts_exactly_one_validation_retry(monkeypatch):
    calls = 0

    def fake_urlopen(_request, timeout):
        del timeout
        nonlocal calls
        calls += 1
        return _chat_response('{"score": 7, "watermark": false}')

    monkeypatch.setattr(lm.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(lm.StructuredOutputError) as caught:
        lm.chat_structured(
            _settings(),
            [{"role": "user", "content": "Inspect"}],
            _ScoreResponse,
            transport="openai",
        )

    assert calls == 2
    assert caught.value.__cause__ is not None


def test_chat_structured_repairs_empty_assistant_content(monkeypatch):
    requests = []
    contents = iter([None, '{"score": 0.5, "watermark": false}'])

    def fake_urlopen(request, timeout):
        del timeout
        requests.append(request)
        return _chat_response(next(contents))

    monkeypatch.setattr(lm.urllib.request, "urlopen", fake_urlopen)

    reply = lm.chat_structured(
        _settings(),
        [{"role": "user", "content": "Inspect"}],
        _ScoreResponse,
        transport="openai",
    )

    assert reply.attempts == 2
    assert reply.value.score == 0.5
    repaired = json.loads(requests[1].data)["messages"]
    assert repaired[-1]["role"] == "user"
    assert "не прошёл JSON Schema" in repaired[-1]["content"]


def test_chat_structured_does_not_retry_transport_failure(monkeypatch):
    calls = 0

    def fail_transport(_request, timeout):
        del timeout
        nonlocal calls
        calls += 1
        raise ValueError("transport failed before a response")

    monkeypatch.setattr(lm.urllib.request, "urlopen", fail_transport)

    with pytest.raises(ValueError, match="transport failed"):
        lm.chat_structured(
            _settings(),
            [{"role": "user", "content": "Inspect"}],
            _ScoreResponse,
            transport="openai",
        )
    assert calls == 1


def _fake_sdk(captured, *, preflight=True, prediction_error=None):
    class Handle:
        def respond(self, chat, **kwargs):
            captured["chat"] = chat
            captured["respond_kwargs"] = kwargs
            if prediction_error is not None:
                raise prediction_error
            return SimpleNamespace(parsed={"score": 0.7, "watermark": False})

    class LlmNamespace:
        def model(self, model, **kwargs):
            captured["model"] = model
            captured["model_kwargs"] = kwargs
            return Handle()

    class Client:
        @staticmethod
        def is_valid_api_host(host):
            captured["preflight_host"] = host
            return preflight

        def __init__(self, host):
            captured["client_host"] = host
            self.llm = LlmNamespace()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            captured["client_closed"] = True
            return False

    return SimpleNamespace(Client=Client)


def test_chat_structured_auto_uses_scoped_sdk(monkeypatch):
    captured = {}
    monkeypatch.setattr(lm, "_load_lmstudio", lambda: _fake_sdk(captured))
    monkeypatch.setattr(
        lm.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("HTTP fallback must not run"),
    )

    reply = lm.chat_structured(
        _settings(lm_unload_delay="75"),
        [{"role": "user", "content": "Inspect"}],
        _ScoreResponse,
        transport="auto",
        max_tokens=128,
        temperature=0.15,
    )

    assert reply == lm.StructuredReply(
        value=_ScoreResponse(score=0.7, watermark=False),
        attempts=1,
        transport="sdk",
    )
    assert captured["preflight_host"] == "127.0.0.1:1234"
    assert captured["client_host"] == "127.0.0.1:1234"
    assert captured["client_closed"] is True
    assert captured["model"] == "test-model"
    assert captured["model_kwargs"] == {"ttl": 75}
    assert captured["respond_kwargs"]["response_format"] == TypeAdapter(
        _ScoreResponse
    ).json_schema()
    assert captured["respond_kwargs"]["config"] == {
        "temperature": 0.15,
        "maxTokens": 128,
    }


def test_sdk_prediction_error_is_never_replayed_over_http(monkeypatch):
    captured = {}
    error = RuntimeError("prediction failed")
    monkeypatch.setattr(
        lm,
        "_load_lmstudio",
        lambda: _fake_sdk(captured, prediction_error=error),
    )
    http_calls = 0

    def forbidden_http(*_args, **_kwargs):
        nonlocal http_calls
        http_calls += 1
        return _chat_response('{"score": 0.1, "watermark": false}')

    monkeypatch.setattr(lm.urllib.request, "urlopen", forbidden_http)

    with pytest.raises(RuntimeError, match="prediction failed"):
        lm.chat_structured(
            _settings(),
            [{"role": "user", "content": "Inspect"}],
            _ScoreResponse,
        )
    assert http_calls == 0


@pytest.mark.parametrize("sdk", [None, "failed-preflight"])
def test_chat_structured_auto_falls_back_before_prediction(monkeypatch, sdk):
    captured = {}
    fake = (
        None
        if sdk is None
        else _fake_sdk(captured, preflight=False)
    )
    monkeypatch.setattr(lm, "_load_lmstudio", lambda: fake)
    monkeypatch.setattr(
        lm.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _chat_response(
            '{"score": 0.6, "watermark": false}'
        ),
    )

    reply = lm.chat_structured(
        _settings(),
        [{"role": "user", "content": "Inspect"}],
        _ScoreResponse,
    )

    assert reply.transport == "openai"
    assert reply.attempts == 1


def test_chat_structured_image_bytes_force_http_without_mutating_messages(
    monkeypatch,
):
    requests = []
    monkeypatch.setattr(
        lm,
        "_load_lmstudio",
        lambda: pytest.fail("Image auto transport must not inspect the SDK"),
    )

    def fake_urlopen(request, timeout):
        del timeout
        requests.append(request)
        return _chat_response('{"score": 0.55, "watermark": false}')

    monkeypatch.setattr(lm.urllib.request, "urlopen", fake_urlopen)
    messages = [{"role": "user", "content": "Inspect this image"}]

    reply = lm.chat_structured(
        _settings(),
        messages,
        _ScoreResponse,
        model="vision-model",
        image_bytes=b"\xff\xd8fixture",
        image_mime="image/jpeg",
    )

    payload = json.loads(requests[0].data)
    parts = payload["messages"][0]["content"]
    assert reply.transport == "openai"
    assert payload["model"] == "vision-model"
    assert parts[0] == {"type": "text", "text": "Inspect this image"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert messages == [{"role": "user", "content": "Inspect this image"}]


def test_chat_structured_rejects_sdk_for_openai_image_messages(monkeypatch):
    monkeypatch.setattr(
        lm,
        "_load_lmstudio",
        lambda: pytest.fail("Image validation must happen before SDK preflight"),
    )
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Inspect"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,AA=="},
                },
            ],
        }
    ]

    with pytest.raises(ValueError, match="transport='openai'"):
        lm.chat_structured(
            _settings(), messages, _ScoreResponse, transport="sdk"
        )


def test_chat_structured_accepts_top_level_type_adapter(monkeypatch):
    monkeypatch.setattr(
        lm.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _chat_response(
            '[{"score": 0.4, "watermark": false}]'
        ),
    )

    reply = lm.chat_structured(
        _settings(),
        [{"role": "user", "content": "Inspect"}],
        TypeAdapter(list[_ScoreResponse]),
        transport="openai",
    )

    assert reply.value == [_ScoreResponse(score=0.4, watermark=False)]


def test_rest_base_normalizes_openai_and_rest_urls():
    assert lm.rest_base("http://localhost:1234/v1/") == "http://localhost:1234/api/v1"
    assert lm.rest_base("http://localhost:1234/api/v1") == "http://localhost:1234/api/v1"


def test_model_management_uses_lm_studio_rest_contract(monkeypatch):
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        seen.append((request, timeout))
        return Response()

    monkeypatch.setattr(lm.urllib.request, "urlopen", fake_urlopen)

    assert lm.load_model("http://lm/v1", "model.gguf", 32768) == {"ok": True}
    assert lm.unload_model("http://lm/v1", "instance-1") == {"ok": True}
    assert seen[0][0].full_url == "http://lm/api/v1/models/load"
    assert json.loads(seen[0][0].data) == {
        "model": "model.gguf",
        "context_length": 32768,
        "flash_attention": True,
        "echo_load_config": True,
    }
    assert seen[0][1] == 600
    assert seen[1][0].full_url == "http://lm/api/v1/models/unload"
    assert json.loads(seen[1][0].data) == {"instance_id": "instance-1"}
    assert seen[1][1] == 120
