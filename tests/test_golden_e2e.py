from pathlib import Path
import urllib.error
import urllib.request

import pytest

from tools import golden_e2e


def test_isolated_settings_copy_only_required_tool_coordinates(tmp_path: Path):
    source = {
        "ffmpeg": "C:/tools/ffmpeg.exe",
        "whisper_cli": "C:/tools/whisper-cli.exe",
        "whisper_model": "C:/models/whisper.bin",
        "lm_url": "http://private-lm:1234/v1",
        "pexels_api_key": "must-not-leak",
        "sfx_enabled": True,
        "export_unify_grade": True,
    }

    result = golden_e2e.build_isolated_settings(source, tmp_path / "projects")

    assert result["output_base"] == str((tmp_path / "projects").resolve())
    assert result["ffmpeg"] == source["ffmpeg"]
    assert result["whisper_cli"] == source["whisper_cli"]
    assert result["whisper_model"] == source["whisper_model"]
    assert result["lm_url"] == "http://127.0.0.1:9/v1"
    assert result["sfx_enabled"] is False
    assert result["export_unify_grade"] is False
    assert "pexels_api_key" not in result


def test_sse_parser_and_terminal_contract():
    text = "\n".join(
        [
            'data: {"event": "connected", "data": {"sub_id": "one"}}',
            "",
            'data: {"event": "gate", "data": {"allowed": true}}',
            "",
            'data: {"event": "done", "data": {"path": "master.mp4"}}',
        ]
    )

    events = golden_e2e.parse_sse_text(text)

    assert [event["event"] for event in events] == ["connected", "gate", "done"]
    assert golden_e2e.require_successful_sse(events, "Export") == {
        "path": "master.mp4"
    }


def test_sse_error_is_a_hard_failure():
    events = golden_e2e.parse_sse_text(
        'data: {"event": "error", "data": {"code": "blocked"}}\n\n'
    )

    with pytest.raises(golden_e2e.GoldenError, match="blocked"):
        golden_e2e.require_successful_sse(events, "Export")


def _valid_manifest_contract():
    media_hash = "a" * 64
    profile = {"name": "delivery", "width": 1920, "height": 1080}
    settings = {"sfx_enabled": False, "export_loudnorm": False}
    edit_contract = {"schema_version": 1, "cards": [{"card_id": "card-1"}]}
    cards = [
        {
            "card_id": "card-1",
            "output_field": "assigned_media",
            "media_sha256": media_hash,
            "variant": {
                "id": "var-1",
                "source": "manual",
                "file_sha256": media_hash,
            },
        }
    ]
    export = {
        "mode": "master",
        "sha256": "c" * 64,
        "size": 1234,
        "profile": profile,
        "settings": settings,
        "settings_fingerprint": golden_e2e.json_content_hash(settings),
        "edit_contract": edit_contract,
        "technical_qc": {"ok": True},
    }
    export["contract_fingerprint"] = golden_e2e.json_content_hash(
        {
            "profile": profile,
            "settings": settings,
            "edit_contract": edit_contract,
            "provenance": cards,
        }
    )
    manifest = {
        "manifest_type": "pipeline-studio.master-export",
        "manifest_id": "master-one",
        "export": export,
        "provenance": {"cards": cards},
    }
    info = {
        "manifest_id": "master-one",
        "sha256": "b" * 64,
        "size": 321,
        "output_sha256": "c" * 64,
    }
    return manifest, info


def test_manifest_contract_recomputes_fingerprints_and_hashes():
    manifest, info = _valid_manifest_contract()

    result = golden_e2e.validate_manifest_contract(
        manifest,
        info,
        sidecar_sha256="b" * 64,
        sidecar_size=321,
        output_sha256="c" * 64,
        output_size=1234,
    )

    assert result["manifest_id"] == "master-one"
    assert result["media_sha256"] == "a" * 64

    manifest["export"]["settings"]["sfx_enabled"] = True
    with pytest.raises(golden_e2e.GoldenError, match="Settings fingerprint"):
        golden_e2e.validate_manifest_contract(
            manifest,
            info,
            sidecar_sha256="b" * 64,
            sidecar_size=321,
            output_sha256="c" * 64,
            output_size=1234,
        )


def test_multipart_and_powershell_escaping_are_safe():
    body = golden_e2e.multipart_body(
        field="file",
        filename="../voice.wav",
        content_type="audio/wav",
        content=b"RIFF-bytes",
        boundary="safe_boundary-1",
    )

    assert b'filename="voice.wav"' in body
    assert b"RIFF-bytes" in body
    assert body.endswith(b"--safe_boundary-1--\r\n")
    assert golden_e2e.powershell_literal("it's.wav") == "'it''s.wav'"


def test_offline_lm_stub_fails_immediately_and_stops_cleanly():
    stub = golden_e2e.OfflineLmStub()
    stub.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(stub.base_url + "/models", timeout=2)
        assert raised.value.code == 503
    finally:
        stub.stop()
