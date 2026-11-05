"""Cloud (OpenAI-compatible) LLM provider: API key, scoping and LM Studio opt-outs."""

import asyncio
import io
import json
import urllib.error
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from backend.routers import diagnostics
from backend.routers import settings as settings_router
from backend.services import pipeline_orch
from core import config as config_module
from core import lm, scenes, stock
from core.config import Settings

CLOUD_URL = "https://api.example-llm.com/v1"


@pytest.fixture(autouse=True)
def _isolated_endpoint(monkeypatch):
    # The configured endpoint is process-global; never leak it between tests.
    monkeypatch.setattr(lm, "_configured", None)


def _cloud(**overrides) -> Settings:
    values = dict(
        lm_url=CLOUD_URL,
        lm_provider="openai_compatible",
        lm_api_key="sk-test-123",
        lm_model="gpt-test",
        lm_auto_unload=True,
        lm_unload_delay="0",
    )
    values.update(overrides)
    return Settings(**values)


class _Response:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return self._body


def _capture(monkeypatch, payload):
    seen = []

    def fake_urlopen(request, timeout):
        seen.append(request)
        return _Response(payload)

    monkeypatch.setattr(lm.urllib.request, "urlopen", fake_urlopen)
    return seen


CHAT_OK = {"choices": [{"message": {"content": '{"answer": "ok"}'}}]}


def test_key_is_sent_only_to_the_configured_origin():
    lm.configure_endpoint(_cloud())

    assert lm.request_headers(CLOUD_URL + "/chat/completions")["Authorization"] == "Bearer sk-test-123"
    # Same host, default port spelled out: still the same origin.
    assert "Authorization" in lm.request_headers("https://API.example-llm.com:443/v1/embeddings")
    for foreign in (
        "http://api.example-llm.com/v1/chat/completions",  # scheme downgrade
        "https://api.example-llm.com.evil.test/v1/chat/completions",
        "https://other.example/v1/chat/completions",
        "not a url",
    ):
        assert "Authorization" not in lm.request_headers(foreign), foreign


def test_lmstudio_provider_never_sends_a_stored_key():
    lm.configure_endpoint(_cloud(lm_provider="lmstudio", lm_url="http://127.0.0.1:1234/v1"))

    headers = lm.request_headers("http://127.0.0.1:1234/v1/chat/completions")

    assert headers == {"Content-Type": "application/json"}


def test_load_and_save_settings_publish_the_endpoint(tmp_path):
    path = tmp_path / "settings.json"
    config_module.save_settings(_cloud(), path)
    assert "Authorization" in lm.request_headers(CLOUD_URL + "/models")

    lm.configure_endpoint(Settings())
    assert "Authorization" not in lm.request_headers(CLOUD_URL + "/models")

    config_module.load_settings(path)
    assert "Authorization" in lm.request_headers(CLOUD_URL + "/models")


def test_cloud_chat_has_auth_no_ttl_and_lenient_schema(monkeypatch):
    seen = _capture(monkeypatch, CHAT_OK)

    class Answer(BaseModel):
        answer: str

    reply = lm.chat_structured(_cloud(), [{"role": "user", "content": "hi"}], Answer, ttl=300)

    assert reply.value.answer == "ok"
    assert reply.transport == "openai"
    request = seen[0]
    payload = json.loads(request.data)
    assert request.get_header("Authorization") == "Bearer sk-test-123"
    assert "ttl" not in payload
    assert payload["response_format"]["json_schema"]["strict"] is False


def test_lmstudio_chat_keeps_ttl_and_strict_schema(monkeypatch):
    seen = _capture(monkeypatch, CHAT_OK)

    lm.chat(_cloud(lm_provider="lmstudio", lm_url="http://127.0.0.1:1234/v1"),
            [{"role": "user", "content": "hi"}], ttl=300)

    payload = json.loads(seen[0].data)
    assert payload["ttl"] == 300
    assert seen[0].get_header("Authorization") is None


def test_cloud_provider_skips_lmstudio_sdk(monkeypatch):
    def forbidden():
        raise AssertionError("cloud mode must not probe the LM Studio SDK")

    monkeypatch.setattr(lm, "_load_lmstudio", forbidden)

    selected, sdk, host = lm._select_structured_transport(_cloud(), "auto", [])

    assert (selected, sdk, host) == ("openai", None, None)
    with pytest.raises(ValueError):
        lm._select_structured_transport(_cloud(), "sdk", [])


def test_cloud_provider_disables_unload_and_ttl_policies():
    settings = _cloud(lm_unload_delay="120")

    assert lm.unload_delay_seconds(settings) is None
    assert lm.ttl_seconds(settings) is None
    assert lm.unload_delay_seconds({"lm_provider": "openai_compatible"}) is None
    assert lm.unload_delay_seconds({"lm_provider": "lmstudio", "lm_unload_delay": "30"}) == 30


def test_url_only_helpers_resolve_provider_and_key(monkeypatch):
    lm.configure_endpoint(_cloud())
    seen = []

    def fake_urlopen(request, timeout):
        seen.append(request)
        return _Response({"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    monkeypatch.setattr(scenes.urllib.request, "urlopen", fake_urlopen)

    vectors = scenes._lm_embeddings(CLOUD_URL, "text-embedding-test", ["hello"])

    assert vectors == [[0.1, 0.2]]
    assert seen[0].get_header("Authorization") == "Bearer sk-test-123"
    # Stock scoring builds a URL-only settings shim; the provider still applies.
    shim = SimpleNamespace(lm_url=CLOUD_URL, lm_auto_unload=True, lm_unload_delay="0")
    assert lm.is_lmstudio(shim) is False
    assert stock._vision_ttl(shim) is None


def test_settings_route_masks_and_preserves_the_lm_key(tmp_path, monkeypatch):
    current = _cloud(output_base=str(tmp_path))
    saved = []
    monkeypatch.setattr(settings_router, "load_settings", lambda: current)
    monkeypatch.setattr(settings_router, "save_settings", saved.append)
    monkeypatch.setattr(settings_router, "list_projects", lambda _path: [])

    shown = asyncio.run(settings_router.get_settings())
    assert shown["lm_api_key"] == ""
    assert shown["lm_api_key_set"] is True

    # The UI sends the masked form back: a blank key must keep the stored one.
    result = asyncio.run(settings_router.update_settings(shown))
    assert result == {"ok": True}
    assert saved[-1].lm_api_key == "sk-test-123"

    asyncio.run(settings_router.update_settings({"lm_api_key": "sk-new"}))
    assert saved[-1].lm_api_key == "sk-new"


def test_settings_route_rejects_unknown_provider(tmp_path, monkeypatch):
    current = Settings(output_base=str(tmp_path))
    monkeypatch.setattr(settings_router, "load_settings", lambda: current)
    monkeypatch.setattr(settings_router, "list_projects", lambda _path: [])

    with pytest.raises(settings_router.HTTPException) as caught:
        asyncio.run(settings_router.update_settings({"lm_provider": "anthropic-native"}))

    assert caught.value.status_code == 422


def test_pipeline_skips_lmstudio_context_load_for_cloud(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("cloud mode must not call LM Studio /models/load")

    monkeypatch.setattr(pipeline_orch.lm_client, "load_model", forbidden)
    logs = []

    result = pipeline_orch._ensure_lm_context(
        {"lm_provider": "openai_compatible", "lm_url": CLOUD_URL, "lm_model": "gpt-test"},
        logs.append,
    )

    assert result is None
    assert any("Облачный" in line for line in logs)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(CLOUD_URL, code, "denied", {}, io.BytesIO(b'{"error": "bad key"}'))


def test_diagnostics_reports_rejected_key(monkeypatch):
    lm.configure_endpoint(_cloud())
    seen = []

    def fake_urlopen(request, timeout):
        seen.append(request)
        raise _http_error(401)

    monkeypatch.setattr(diagnostics.urllib.request, "urlopen", fake_urlopen)

    result = diagnostics._check_lm(_cloud())

    assert result.ok is False
    assert "API-ключ" in result.message
    assert seen[0].get_header("Authorization") == "Bearer sk-test-123"


def test_diagnostics_treats_unlisted_cloud_model_as_a_hint(monkeypatch):
    monkeypatch.setattr(
        diagnostics.urllib.request, "urlopen",
        lambda request, timeout: _Response({"data": [{"id": "models/gpt-test"}]}),
    )

    result = diagnostics._check_lm(_cloud())

    assert result.ok is True
    assert "нет в списке" in result.message


def test_cloud_inference_probe_sends_no_ttl(monkeypatch):
    seen = []

    def fake_urlopen(request, timeout):
        seen.append(request)
        if request.get_method() == "GET":
            return _Response({"data": [{"id": "gpt-test"}]})
        return _Response({"choices": [{"message": {"content": "OK"}}]})

    monkeypatch.setattr(diagnostics.urllib.request, "urlopen", fake_urlopen)

    result = diagnostics._check_lm_inference(_cloud())

    assert result.ok is True
    assert "ttl" not in json.loads(seen[-1].data)


@pytest.mark.parametrize(("code", "fragment"), [(401, "API-ключ"), (403, "API-ключ"), (429, "HTTP 429")])
def test_explain_error_names_cloud_failures(code, fragment):
    assert fragment in lm.explain_error(_http_error(code))
