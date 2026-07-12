"""Tests for core.lm — the shared LM Studio client (strangler slice #1)."""

import json
from types import SimpleNamespace

import pytest

from core import lm


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
