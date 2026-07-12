import json
from types import SimpleNamespace

import pytest

from backend.routers import diagnostics
from tools import integration_smoke


def test_run_checks_isolates_failures_and_preserves_target_order():
    checks = {
        "ffmpeg": lambda: {
            "target": "ffmpeg", "ok": True, "message": "ready", "detail": ""
        },
        "comfy": lambda: (_ for _ in ()).throw(RuntimeError("offline")),
    }
    report = integration_smoke.run_checks(
        SimpleNamespace(),
        ["ffmpeg", "comfy", "ffmpeg"],
        checkers=checks,
    )

    assert list(report["results"]) == ["ffmpeg", "comfy"]
    assert report["results"]["ffmpeg"]["ok"] is True
    assert report["results"]["comfy"]["ok"] is False
    assert report["results"]["comfy"]["detail"] == "offline"


def test_run_checks_rejects_unknown_target():
    with pytest.raises(ValueError, match="Unknown"):
        integration_smoke.run_checks(
            SimpleNamespace(),
            ["unknown"],
            checkers={},
        )


@pytest.mark.parametrize(
    ("available_ids", "expected_ok"),
    [
        (["chat-model-q4"], False),
        (["chat-model-q4", "chat-model"], True),
    ],
)
def test_passive_lm_requires_exact_configured_model_id(
    monkeypatch, available_ids, expected_ok
):
    requested_urls = []

    def fake_get(url, timeout=5):
        requested_urls.append((url, timeout))
        return True, json.dumps({"data": [{"id": item} for item in available_ids]})

    monkeypatch.setattr(diagnostics, "_http_get", fake_get)
    result = diagnostics._check_lm(
        SimpleNamespace(lm_url="http://lm.test/v1", lm_model="chat-model")
    )

    assert result.ok is expected_ok
    assert requested_urls == [("http://lm.test/v1/models", 5)]


def test_passive_embed_uses_only_model_list_get(monkeypatch):
    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, limit=-1):
            assert limit == 4000
            return json.dumps({"data": [{"id": "embed-model"}]}).encode("utf-8")

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(diagnostics.urllib.request, "urlopen", fake_urlopen)
    result = diagnostics._check_embed_available(
        SimpleNamespace(
            lm_url="http://lm.test/v1",
            lm_embed_model="embed-model",
        )
    )

    assert result.ok is True
    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url == "http://lm.test/v1/models"
    assert request.get_method() == "GET"
    assert request.data is None
    assert timeout == 5


def test_active_flag_selects_chat_and_embedding_inference_probes(
    monkeypatch, capsys
):
    calls = []
    settings = SimpleNamespace()

    def active_lm(actual_settings):
        assert actual_settings is settings
        calls.append("lm-inference")
        return diagnostics.CheckResult(
            target="lm", ok=True, message="active lm"
        )

    def active_embed(actual_settings):
        assert actual_settings is settings
        calls.append("embed-inference")
        return diagnostics.CheckResult(
            target="embed", ok=True, message="active embed"
        )

    def unexpected_passive_probe(_settings):
        raise AssertionError("passive probe selected in --active mode")

    monkeypatch.setattr(integration_smoke, "load_settings", lambda: settings)
    monkeypatch.setattr(diagnostics, "_check_lm_inference", active_lm)
    monkeypatch.setattr(diagnostics, "_check_embed", active_embed)
    monkeypatch.setattr(diagnostics, "_check_lm", unexpected_passive_probe)
    monkeypatch.setattr(
        diagnostics, "_check_embed_available", unexpected_passive_probe
    )

    exit_code = integration_smoke.main(
        ["--targets", "lm", "embed", "--active"]
    )

    assert exit_code == 0
    assert sorted(calls) == ["embed-inference", "lm-inference"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["active"] is True
    assert set(payload["results"]) == {"lm", "embed"}
