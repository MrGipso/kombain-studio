"""comfy.org API key for ComfyUI partner/API ("cloud") nodes.

The key must reach ComfyUI in ``extra_data.api_key_comfy_org`` on every queued
prompt when it is configured, and nowhere else: not in settings hashes, export
manifests, variant records, job errors or diagnostics.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.routers import diagnostics
from backend.routers import export_router
from backend.routers import settings as settings_router
from backend.services import pipeline_orch
from core import asset_variants, comfy, export_manifest
from core import comfy_generation as generation
from core.config import Settings

SECRET = "comfyui-0123456789abcdef-must-not-leak"


def _capture_requests(monkeypatch, response=None):
    seen: list[dict] = []

    def fake_request(url, payload=None, timeout=30):
        seen.append({"url": url, "payload": payload, "timeout": timeout})
        return {"prompt_id": "prompt-1"} if response is None else response(payload)

    monkeypatch.setattr(comfy, "comfy_request_json", fake_request)
    return seen


# --------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------


@pytest.mark.parametrize("api_key", ["", "   "])
def test_queue_prompt_without_key_sends_the_unchanged_payload(monkeypatch, api_key):
    seen = _capture_requests(monkeypatch)

    assert comfy.comfy_queue_prompt("http://comfy", {"1": {}}, api_key=api_key) == "prompt-1"
    assert comfy.comfy_queue_prompt("http://comfy", {"1": {}}) == "prompt-1"

    for request in seen:
        assert set(request["payload"]) == {"prompt", "client_id"}


def test_queue_prompt_sends_the_key_in_extra_data(monkeypatch):
    seen = _capture_requests(monkeypatch)

    comfy.comfy_queue_prompt("http://comfy/", {"1": {}}, api_key=f"  {SECRET}\n")

    payload = seen[0]["payload"]
    assert seen[0]["url"] == "http://comfy/prompt"
    assert payload["prompt"] == {"1": {}}
    assert payload["client_id"]
    assert payload["extra_data"] == {"api_key_comfy_org": SECRET}
    assert comfy.COMFY_ORG_API_KEY_FIELD == "api_key_comfy_org"


def test_client_facade_forwards_the_key(monkeypatch):
    seen = _capture_requests(monkeypatch)

    comfy.ComfyClient("http://comfy").queue_prompt({"1": {}}, api_key=SECRET)
    comfy.ComfyClient("http://comfy").queue_prompt({"1": {}})

    assert seen[0]["payload"]["extra_data"] == {"api_key_comfy_org": SECRET}
    assert "extra_data" not in seen[1]["payload"]


def test_queue_errors_never_echo_the_key(monkeypatch):
    # A server (or proxy) that echoes the request back without a prompt id.
    _capture_requests(monkeypatch, response=lambda payload: {"error": "x", "echo": payload})
    with pytest.raises(RuntimeError, match="did not return prompt_id") as caught:
        comfy.comfy_queue_prompt("http://comfy", {}, api_key=SECRET)
    assert SECRET not in str(caught.value)
    assert "***" in str(caught.value)

    def failing(url, payload=None, timeout=30):
        raise urllib.error.URLError(f"rejected {json.dumps(payload)}")

    monkeypatch.setattr(comfy, "comfy_request_json", failing)
    with pytest.raises(RuntimeError) as caught:
        comfy.comfy_queue_prompt("http://comfy", {}, api_key=SECRET)
    assert SECRET not in str(caught.value)
    assert caught.value.__suppress_context__ is True


def test_queue_errors_without_the_key_keep_their_type(monkeypatch):
    def failing(url, payload=None, timeout=30):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(comfy, "comfy_request_json", failing)
    with pytest.raises(urllib.error.URLError):
        comfy.comfy_queue_prompt("http://comfy", {}, api_key=SECRET)


# --------------------------------------------------------------------------
# Generators pass the snapshot key on every queue call
# --------------------------------------------------------------------------


def _record_queue(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def queue(base_url, workflow, **kwargs):
        calls.append({"base_url": base_url, "workflow": workflow, **kwargs})
        return "prompt-1"

    monkeypatch.setattr(generation, "comfy_queue_prompt", queue)
    return calls


def _stub_history(monkeypatch, kind: str) -> None:
    monkeypatch.setattr(
        generation,
        "comfy_wait_for_history",
        lambda *_args, **_kwargs: {"outputs": {"9": {kind: [{"filename": "out.bin"}]}}},
    )


def _run_image(monkeypatch, tmp_path: Path, settings):
    card = SimpleNamespace(image_model="krea2_turbo", image_prompt="a frame", shot_index=1)
    monkeypatch.setattr(
        generation, "prepare_image_workflow", lambda *_args, **_kwargs: {"krea": {}}
    )
    _stub_history(monkeypatch, "images")
    monkeypatch.setattr(
        generation, "comfy_download_output", lambda *_args: tmp_path / "image.png"
    )
    generation.generate_comfy_image("http://comfy", tmp_path, card, settings=settings)


def _run_reference(monkeypatch, tmp_path: Path, settings):
    workflow_file = tmp_path / "reference.json"
    workflow_file.write_text("{}", encoding="utf-8")
    reference = tmp_path / "canonical.png"
    reference.write_bytes(b"canonical")
    card = SimpleNamespace(image_model="krea2_turbo", image_prompt="a frame", shot_index=1)
    monkeypatch.setattr(
        generation,
        "reference_workflow_status",
        lambda *_args: {"enabled": True, "ok": True, "path": str(workflow_file)},
    )
    monkeypatch.setattr(generation, "comfy_upload_image", lambda *_args: "canonical.png")
    monkeypatch.setattr(
        generation,
        "prepare_reference_image_workflow",
        lambda *_args, **_kwargs: {"reference": {}},
    )
    _stub_history(monkeypatch, "images")
    monkeypatch.setattr(
        generation, "comfy_download_output", lambda *_args: tmp_path / "image.png"
    )
    generation.generate_comfy_image(
        "http://comfy", tmp_path, card, settings=settings, reference_image=reference
    )


def _run_ltx(monkeypatch, tmp_path: Path, settings):
    image = tmp_path / "input.png"
    image.write_bytes(b"image")
    card = SimpleNamespace(
        image_output=str(image),
        video_prompt="slow camera move",
        image_prompt="",
        shot_index=2,
        start="00:00:00",
        end="00:00:04",
        priority="medium",
        ltx_draft=False,
    )
    monkeypatch.setattr(generation, "comfy_upload_image", lambda *_args: "input.png")
    monkeypatch.setattr(generation, "project_video_style_id", lambda *_args: "default")
    monkeypatch.setattr(
        generation, "get_video_style", lambda *_args: SimpleNamespace(negative_prompt="")
    )
    monkeypatch.setattr(
        generation, "prepare_ltx_workflow", lambda *_args, **_kwargs: {"ltx": {}}
    )
    _stub_history(monkeypatch, "videos")
    monkeypatch.setattr(
        generation, "comfy_download_output", lambda *_args: tmp_path / "video.mp4"
    )
    generation.generate_comfy_ltx_video(
        "http://comfy", tmp_path, card, draft=False, settings=settings
    )


def _run_music(monkeypatch, tmp_path: Path, settings):
    monkeypatch.setattr(
        generation, "prepare_music_workflow", lambda *_args, **_kwargs: {"music": {}}
    )
    _stub_history(monkeypatch, "audio")
    monkeypatch.setattr(
        generation, "comfy_download_file", lambda _url, _item, target: target.write_bytes(b"a")
    )
    monkeypatch.setattr(generation, "_limit_music_peak", lambda *_args: None)
    generation.generate_comfy_music("http://comfy", tmp_path, "ambient", settings=settings)


GENERATORS = {
    "image": (_run_image, "krea"),
    "reference": (_run_reference, "reference"),
    "ltx": (_run_ltx, "ltx"),
    "music": (_run_music, "music"),
}


@pytest.mark.parametrize("name", sorted(GENERATORS))
def test_every_generator_queues_with_the_snapshot_key(monkeypatch, tmp_path, name):
    run, workflow_marker = GENERATORS[name]
    calls = _record_queue(monkeypatch)

    run(monkeypatch, tmp_path, Settings(comfy_api_key=f" {SECRET} "))

    assert len(calls) == 1
    assert calls[0]["api_key"] == SECRET
    assert workflow_marker in calls[0]["workflow"]


@pytest.mark.parametrize("name", sorted(GENERATORS))
def test_generators_without_a_key_keep_the_two_argument_call(monkeypatch, tmp_path, name):
    # Existing monkeypatches of comfy_queue_prompt accept only positional
    # arguments, so the no-key path must not pass api_key at all.
    run, _marker = GENERATORS[name]
    calls: list[tuple] = []
    monkeypatch.setattr(
        generation, "comfy_queue_prompt", lambda *args: calls.append(args) or "prompt-1"
    )

    run(monkeypatch, tmp_path, Settings())

    assert len(calls) == 1 and len(calls[0]) == 2


def test_dict_snapshots_supply_the_key_too():
    assert generation.comfy_api_key({"comfy_api_key": SECRET}) == SECRET
    assert generation.comfy_api_key(SimpleNamespace(comfy_api_key=None)) == ""
    assert generation.comfy_api_key({}) == ""


def test_generation_job_error_does_not_carry_the_key(monkeypatch, tmp_path):
    """End to end through the real queue call: the job sees a clean error."""

    def echo(url, payload=None, timeout=30):
        raise urllib.error.URLError(f"Sign In Required; request was {json.dumps(payload)}")

    monkeypatch.setattr(comfy, "comfy_request_json", echo)
    monkeypatch.setattr(
        generation, "prepare_music_workflow", lambda *_args, **_kwargs: {"1": {}}
    )

    with pytest.raises(RuntimeError) as caught:
        generation.generate_comfy_music(
            "http://comfy", tmp_path, "ambient", settings=Settings(comfy_api_key=SECRET)
        )
    assert "Sign In Required" in str(caught.value)
    assert SECRET not in str(caught.value)


# --------------------------------------------------------------------------
# Settings route: masked, preserved, never invalidates rendered stages
# --------------------------------------------------------------------------


def test_settings_route_masks_and_preserves_the_comfy_key(tmp_path, monkeypatch):
    current = Settings(output_base=str(tmp_path), comfy_api_key=SECRET)
    saved: list[Settings] = []
    monkeypatch.setattr(settings_router, "load_settings", lambda: current)
    monkeypatch.setattr(settings_router, "save_settings", saved.append)
    monkeypatch.setattr(settings_router, "list_projects", lambda _path: [])

    shown = asyncio.run(settings_router.get_settings())
    assert shown["comfy_api_key"] == ""
    assert shown["comfy_api_key_set"] is True
    assert SECRET not in json.dumps(shown)

    # The UI sends the masked form back: a blank key keeps the stored one.
    assert asyncio.run(settings_router.update_settings(shown)) == {"ok": True}
    assert saved[-1].comfy_api_key == SECRET

    asyncio.run(settings_router.update_settings({"comfy_api_key": "comfyui-new"}))
    assert saved[-1].comfy_api_key == "comfyui-new"


def test_unset_comfy_key_is_reported_as_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings_router, "load_settings", lambda: Settings(output_base=str(tmp_path))
    )
    shown = asyncio.run(settings_router.get_settings())
    assert shown["comfy_api_key"] == ""
    assert shown["comfy_api_key_set"] is False


def test_changing_only_the_comfy_key_invalidates_no_stage(tmp_path, monkeypatch):
    initial = Settings(output_base=str(tmp_path), comfy_api_key="old-key")

    class RecordingProject:
        def __init__(self):
            self.dir = tmp_path / "recording-project"
            self.dir.mkdir()
            self.calls: list[str] = []

        def invalidate_stage(self, stage: str, reason: str | None = None):
            self.calls.append(stage)

    project = RecordingProject()
    saved: list[Settings] = []
    monkeypatch.setattr(settings_router, "load_settings", lambda: initial)
    monkeypatch.setattr(settings_router, "save_settings", saved.append)
    monkeypatch.setattr(settings_router, "list_projects", lambda _base: [project])
    monkeypatch.setattr(settings_router, "open_project", lambda *_args, **_kwargs: project)

    asyncio.run(settings_router.update_settings({"comfy_api_key": "new-key"}))

    assert saved[-1].comfy_api_key == "new-key"
    assert project.calls == []


# --------------------------------------------------------------------------
# No leak into hashes, manifests, records or diagnostics
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(settings_router.SECRET_KEYS))
def test_every_secret_setting_is_dropped_by_the_name_based_redactors(key):
    # _settings_hash and the export manifest redact by key name, so a secret
    # whose name they do not recognise would be hashed or written verbatim.
    with_secret = Settings(**{key: SECRET})
    assert asset_variants._settings_hash(with_secret) == asset_variants._settings_hash(Settings())
    assert key not in export_manifest._redact_secrets(asdict(with_secret))


def test_settings_hash_ignores_the_comfy_key_for_dataclass_and_dict_snapshots():
    plain = asset_variants._settings_hash(Settings())
    assert asset_variants._settings_hash(Settings(comfy_api_key=SECRET)) == plain
    assert asset_variants._settings_hash(Settings(comfy_api_key="other")) == plain
    as_dict = asdict(Settings())
    assert asset_variants._settings_hash({**as_dict, "comfy_api_key": SECRET}) == (
        asset_variants._settings_hash(as_dict)
    )
    assert export_manifest.settings_fingerprint({**as_dict, "comfy_api_key": SECRET}) == (
        export_manifest.settings_fingerprint(as_dict)
    )


def test_stage_input_hashes_ignore_the_comfy_key():
    scenes = [{"index": 0, "text": "hello"}]
    base = asdict(Settings())
    assert pipeline_orch.shot_stage_input_hash(scenes, {**base, "comfy_api_key": SECRET}) == (
        pipeline_orch.shot_stage_input_hash(scenes, base)
    )


def test_variant_manifest_never_contains_the_key(tmp_path):
    media = tmp_path / "shot.png"
    media.write_bytes(b"frame")

    variant = asset_variants.record_variant(
        tmp_path,
        card_index=0,
        card_id="card-1",
        shot_index=1,
        source="comfy",
        mode="image",
        output_field="image_output",
        file_path=media,
        settings=Settings(comfy_api_key=SECRET),
    )

    assert variant["settings_hash"] == asset_variants._settings_hash(Settings())
    assert SECRET not in asset_variants.manifest_path(tmp_path).read_text(encoding="utf-8")


def test_master_export_manifest_never_contains_the_key(tmp_path, monkeypatch):
    media = tmp_path / "shot.png"
    media.write_bytes(b"frame")
    card = {
        "card_id": "card-1",
        "shot_index": 1,
        "status": "ready",
        "approved": True,
        "route": "image",
        "visual_type": "ai_image",
        "image_output": str(media),
        "video_output": "",
        "assigned_media": "",
        "ltx_draft": False,
    }
    asset_variants.record_variant(
        tmp_path,
        card_index=0,
        card_id="card-1",
        shot_index=1,
        source="comfy",
        mode="image",
        output_field="image_output",
        file_path=media,
        settings=Settings(comfy_api_key=SECRET),
    )
    provenance = export_manifest.evaluate_master_provenance(tmp_path, [card])
    output = tmp_path / "master_001.mp4"
    output.write_bytes(b"rendered master")
    monkeypatch.setattr(
        export_manifest,
        "tool_contract",
        lambda _ffmpeg: {"application": "pipeline-studio", "ffmpeg": {"version": "test"}},
    )

    # Even a settings contract that carries the full snapshot is redacted.
    result = export_manifest.write_master_export_manifest(
        output,
        project_id="project-1",
        project_name="Project One",
        project_schema_version=1,
        provenance=provenance,
        profile={"name": "master"},
        settings_contract=asdict(Settings(comfy_api_key=SECRET)),
        technical_qc={"ok": True},
    )

    sidecar = output.parent / result["path"]
    assert SECRET not in sidecar.read_text(encoding="utf-8")
    index = output.parent / export_manifest.MASTER_EXPORT_INDEX_FILE
    assert SECRET not in index.read_text(encoding="utf-8")

    # The real export route builds its contract from an allowlist.
    project = SimpleNamespace(
        effective_settings=lambda: {**asdict(Settings()), "comfy_api_key": SECRET},
    )
    assert "comfy_api_key" not in export_router._export_settings_contract(project)


def test_comfy_diagnostics_never_show_the_key(monkeypatch):
    monkeypatch.setattr(diagnostics, "_http_get", lambda *_args, **_kwargs: (True, "{}"))
    monkeypatch.setattr(
        diagnostics,
        "validate_configured_workflows",
        lambda *_args: {
            "ok": True,
            "available_node_types": 10,
            "workflows": {"krea": {"ok": True}},
        },
    )

    result = diagnostics._check_comfy(Settings(comfy_api_key=SECRET))

    assert result.ok is True
    assert SECRET not in result.model_dump_json()
