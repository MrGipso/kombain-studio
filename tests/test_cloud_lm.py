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
    # The configured endpoint, usage counter and format cache are
    # process-global; never leak them between tests or really sleep.
    monkeypatch.setattr(lm, "_configured", None)
    monkeypatch.setattr(lm, "_usage_day", None)
    monkeypatch.setattr(lm, "_usage_count", 0)
    monkeypatch.setattr(lm, "_format_modes", {})
    monkeypatch.setattr(lm, "_sleep", lambda _seconds: None)


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

    monkeypatch.setattr(scenes.lm_client.urllib.request, "urlopen", fake_urlopen)

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


# --- retries, daily cap, structured-output fallback -------------------------


def _scripted(monkeypatch, steps):
    """urlopen that replays ``steps``: an int is an HTTP error, a dict a reply."""
    seen = []
    queue = list(steps)

    def fake_urlopen(request, timeout):
        seen.append(request)
        step = queue.pop(0)
        if isinstance(step, tuple):
            code, body, headers = step
            raise urllib.error.HTTPError(request.full_url, code, "error", headers, io.BytesIO(body))
        if isinstance(step, int):
            raise urllib.error.HTTPError(request.full_url, step, "error", {}, io.BytesIO(b"{}"))
        return _Response(step)

    monkeypatch.setattr(lm.urllib.request, "urlopen", fake_urlopen)
    return seen


def _record_sleeps(monkeypatch):
    sleeps = []
    monkeypatch.setattr(lm, "_sleep", sleeps.append)
    return sleeps


def test_cloud_retries_rate_limit_honouring_retry_after(monkeypatch):
    seen = _scripted(monkeypatch, [(429, b"{}", {"Retry-After": "3"}), CHAT_OK])
    sleeps = _record_sleeps(monkeypatch)

    assert lm.chat(_cloud(), [{"role": "user", "content": "hi"}]) == '{"answer": "ok"}'

    assert len(seen) == 2
    assert sleeps == [3.0]
    # One logical request, however many attempts it took.
    assert lm.cloud_requests_today() == 1


def test_cloud_gives_up_after_bounded_backoff(monkeypatch):
    seen = _scripted(monkeypatch, [503, 503, 503, 503])
    sleeps = _record_sleeps(monkeypatch)

    with pytest.raises(urllib.error.HTTPError) as caught:
        lm.chat(_cloud(), [{"role": "user", "content": "hi"}])

    assert caught.value.code == 503
    assert len(seen) == 4
    assert sleeps == [2.0, 4.0, 8.0]


def test_retry_after_is_capped_and_accepts_http_dates():
    def error(value):
        return urllib.error.HTTPError(CLOUD_URL, 429, "slow down", {"Retry-After": value}, io.BytesIO(b""))

    assert lm._retry_delay(error("3600"), 1) == lm._MAX_RETRY_WAIT_SECONDS
    assert lm._retry_delay(error("Wed, 21 Oct 2015 07:28:00 GMT"), 1) == 0.5  # in the past
    assert lm._retry_delay(error("soon"), 2) == 4.0


@pytest.mark.parametrize("code", [400, 401, 404])
def test_client_errors_are_not_retried(monkeypatch, code):
    seen = _scripted(monkeypatch, [code])

    with pytest.raises(urllib.error.HTTPError):
        lm.chat(_cloud(), [{"role": "user", "content": "hi"}])

    assert len(seen) == 1


def test_lmstudio_keeps_single_attempt_and_no_count(monkeypatch):
    seen = _scripted(monkeypatch, [503])
    local = _cloud(lm_provider="lmstudio", lm_url="http://127.0.0.1:1234/v1")

    with pytest.raises(urllib.error.HTTPError):
        lm.chat(local, [{"role": "user", "content": "hi"}])

    assert len(seen) == 1
    assert lm.cloud_requests_today() == 0


def test_daily_limit_stops_requests_before_they_are_sent(monkeypatch):
    seen = _scripted(monkeypatch, [CHAT_OK, CHAT_OK])
    settings = _cloud(lm_cloud_daily_limit=2)

    lm.chat(settings, [{"role": "user", "content": "1"}])
    lm.chat(settings, [{"role": "user", "content": "2"}])
    with pytest.raises(lm.CloudRequestLimitError) as caught:
        lm.chat(settings, [{"role": "user", "content": "3"}])

    assert len(seen) == 2
    assert "лимит" in lm.explain_error(caught.value)


def test_daily_limit_applies_to_url_only_helpers(monkeypatch):
    lm.configure_endpoint(_cloud(lm_cloud_daily_limit=1))
    _scripted(monkeypatch, [{"data": [{"index": 0, "embedding": [1.0]}]}])

    scenes._lm_embeddings(CLOUD_URL, "embed", ["a"])
    with pytest.raises(lm.CloudRequestLimitError):
        scenes._lm_embeddings(CLOUD_URL, "embed", ["b"])


class _Answer(BaseModel):
    answer: str


_SCHEMA_REJECTED = (400, b'{"error": {"message": "response_format json_schema is not supported"}}', {})


def test_structured_output_falls_back_to_json_object_and_remembers(monkeypatch):
    seen = _scripted(monkeypatch, [_SCHEMA_REJECTED, CHAT_OK, CHAT_OK])
    messages = [{"role": "system", "content": "Будь краток."}, {"role": "user", "content": "hi"}]

    first = lm.chat_structured(_cloud(), messages, _Answer)
    second = lm.chat_structured(_cloud(), messages, _Answer)

    assert first.value.answer == second.value.answer == "ok"
    assert len(seen) == 3  # the rejected mode is not retried on the next call
    fallback = json.loads(seen[1].data)
    assert fallback["response_format"] == {"type": "json_object"}
    system = fallback["messages"][0]
    assert system["role"] == "system"
    assert system["content"].startswith("Будь краток.")
    assert "JSON Schema" in system["content"]
    assert json.loads(seen[2].data)["response_format"] == {"type": "json_object"}


def test_array_schema_skips_json_object_mode(monkeypatch):
    reply = {"choices": [{"message": {"content": '[{"answer": "ok"}]'}}]}
    seen = _scripted(monkeypatch, [_SCHEMA_REJECTED, reply])

    result = lm.chat_structured(_cloud(), [{"role": "user", "content": "hi"}], list[_Answer])

    assert [item.answer for item in result.value] == ["ok"]
    fallback = json.loads(seen[1].data)
    assert "response_format" not in fallback
    assert fallback["messages"][0]["role"] == "system"


def test_unrelated_bad_request_is_raised_with_its_body(monkeypatch):
    _scripted(monkeypatch, [(400, b'{"error": "model gpt-test does not exist"}', {})])

    with pytest.raises(urllib.error.HTTPError) as caught:
        lm.chat_structured(_cloud(), [{"role": "user", "content": "hi"}], _Answer)

    assert "does not exist" in lm.explain_error(caught.value)


def test_settings_route_reports_usage_as_read_only(tmp_path, monkeypatch):
    current = _cloud(output_base=str(tmp_path))
    saved = []
    monkeypatch.setattr(settings_router, "load_settings", lambda: current)
    monkeypatch.setattr(settings_router, "save_settings", saved.append)
    monkeypatch.setattr(settings_router, "list_projects", lambda _path: [])
    monkeypatch.setattr(lm, "cloud_requests_today", lambda: 7)

    shown = asyncio.run(settings_router.get_settings())
    assert shown["lm_cloud_requests_today"] == 7

    shown["lm_cloud_daily_limit"] = "500"  # the number input may send a string
    assert asyncio.run(settings_router.update_settings(shown)) == {"ok": True}
    assert saved[-1].lm_cloud_daily_limit == 500
