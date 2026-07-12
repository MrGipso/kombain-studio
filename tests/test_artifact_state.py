import json
from pathlib import Path

import core.project as project_module
from core.config import Settings
from core.project import (
    CURRENT_MANIFEST_SCHEMA_VERSION,
    CURRENT_PROJECT_SCHEMA_VERSION,
    PIPELINE_MANIFEST_FILE,
    PIPELINE_STAGE_DEPENDENCIES,
    PIPELINE_STAGE_OPTIONAL_DEPENDENCIES,
    PIPELINE_STAGE_ORDER,
    Project,
    content_hash,
    open_project,
)


def test_legacy_project_derives_state_without_writing_migration(tmp_path):
    project_dir = tmp_path / "legacy"
    project_dir.mkdir()
    original_meta = {"name": "Legacy", "preset": "universal", "custom_extension": {"keep": True}}
    meta_path = project_dir / ".project.json"
    meta_path.write_text(json.dumps(original_meta), encoding="utf-8")
    (project_dir / "01_transcript.txt").write_text("hello", encoding="utf-8")
    (project_dir / "02_scenes.json").write_text("[]", encoding="utf-8")
    original_bytes = meta_path.read_bytes()

    project = open_project(project_dir)

    assert project is not None
    assert project.schema_version == 0
    assert project.stage_state("transcript")["status"] == "completed"
    assert project.stage_state("scenes")["status"] == "completed"
    assert project.stage_state("shot_list")["status"] == "pending"
    assert not (project_dir / PIPELINE_MANIFEST_FILE).exists()
    assert meta_path.read_bytes() == original_bytes

    project.save_meta()
    saved_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert saved_meta["schema_version"] == CURRENT_PROJECT_SCHEMA_VERSION
    assert saved_meta["custom_extension"] == {"keep": True}


def test_content_hash_is_stable_and_tracks_file_content(tmp_path):
    assert content_hash({"b": [2, 3], "a": 1}) == content_hash({"a": 1, "b": [2, 3]})
    assert content_hash("hello") == content_hash(b"hello")

    artifact = tmp_path / "artifact.txt"
    artifact.write_text("version one", encoding="utf-8")
    first = content_hash(artifact)
    artifact.write_text("version two", encoding="utf-8")
    assert content_hash(artifact) != first


def test_changed_stage_invalidates_completed_downstream_stages(tmp_path):
    project = Project(name="demo", dir=tmp_path)
    for stage in PIPELINE_STAGE_ORDER:
        project.mark_stage_completed(stage, content=f"{stage}-v1")

    changed = project.mark_stage_completed("scenes", content="scenes-v2")
    states = project.stage_states()

    assert changed["status"] == "completed"
    assert changed["revision"] == 2
    assert states["transcript"]["status"] == "completed"
    assert states["scenes"]["status"] == "completed"
    for stage in ("shots", "assets", "timeline", "validation", "export"):
        assert states[stage]["status"] == "stale"
        assert states[stage]["invalidated_by"] == "scenes"
        assert states[stage]["invalidated_at"]

    # Explicit invalidation is transitive and accepts an artifact-key alias.
    invalidated = project.invalidate_downstream("transcript_segments", reason="edited transcript")
    assert invalidated == ["scenes", "shots", "assets", "timeline", "validation", "export"]
    assert project.stage_state("scenes")["error"] == "edited transcript"


def test_invalidate_stage_marks_itself_and_downstream_stale(tmp_path):
    project = Project(name="demo", dir=tmp_path)
    for stage in ("transcript", "scenes", "shots", "assets", "timeline", "validation", "export"):
        project.mark_stage_completed(stage, content=f"{stage}-v1")

    invalidated = project.invalidate_stage("timeline", reason="audio mix changed")

    assert invalidated == ["timeline", "validation", "export"]
    assert project.stage_state("timeline")["status"] == "stale"
    assert project.stage_state("timeline")["invalidated_by"] == "timeline"
    assert project.stage_state("validation")["status"] == "stale"


def test_script_is_optional_for_transcript_but_invalidates_scene_consumers(tmp_path):
    assert PIPELINE_STAGE_DEPENDENCIES["transcript"] == ()
    assert PIPELINE_STAGE_OPTIONAL_DEPENDENCIES["scenes"] == ("script",)

    project = Project(name="demo", dir=tmp_path)
    project.mark_stage_completed("transcript", content="uploaded transcript")
    project.mark_stage_completed("scenes", content="scenes without a script")

    project.mark_stage_completed("script", content="new authored script")

    assert project.stage_state("transcript")["status"] == "completed"
    assert project.stage_state("scenes")["status"] == "stale"
    assert project.stage_state("scenes")["invalidated_by"] == "script"


def test_failed_stage_is_persisted_with_revision_and_timestamp(tmp_path):
    project = Project(name="demo", dir=tmp_path)

    state = project.mark_stage_failed("asset_board", "generator unavailable", input_hash="input-1")

    assert state["status"] == "failed"
    assert state["revision"] == 1
    assert state["failed_at"]
    assert state["error"] == "generator unavailable"
    assert project.stage_state("assets")["input_hash"] == "input-1"


def test_manifest_write_uses_atomic_replace(tmp_path, monkeypatch):
    project = Project(name="demo", dir=tmp_path)
    original_replace = project_module.os.replace
    calls: list[tuple[Path, Path]] = []

    def recording_replace(source, destination):
        calls.append((Path(source), Path(destination)))
        original_replace(source, destination)

    monkeypatch.setattr(project_module.os, "replace", recording_replace)
    project.mark_stage_completed("transcript", content="hello")

    assert len(calls) == 1
    source, destination = calls[0]
    assert destination == project.manifest_path
    assert source.parent == project.manifest_path.parent
    assert source.name.startswith(project.manifest_path.name + ".")
    assert not source.exists()
    assert not list(tmp_path.glob(f"{PIPELINE_MANIFEST_FILE}.*.tmp"))
    manifest = json.loads(project.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == CURRENT_MANIFEST_SCHEMA_VERSION
    assert manifest["stage_order"] == list(PIPELINE_STAGE_ORDER)
    assert manifest["stage_dependencies"]["transcript"] == []
    assert manifest["stage_dependencies"]["scenes"] == ["transcript"]
    assert manifest["stage_optional_dependencies"]["scenes"] == ["script"]
    assert manifest["stages"]["transcript"]["status"] == "completed"


def test_effective_settings_applies_project_values_then_overrides(tmp_path):
    settings = Settings(lm_model="base-model", whisper_language="auto")
    metadata = {
        "video_style": "cinematic",
        "seg_min_seconds": 12,
        "shot_mode": "hybrid",
        "unrelated": "ignored",
    }
    project = Project(name="demo", dir=tmp_path, settings=settings, metadata=metadata)

    effective = project.effective_settings({"lm_model": "request-model", "seg_min_seconds": 8})

    assert effective["whisper_language"] == "auto"
    assert effective["video_style"] == "cinematic"
    assert effective["shot_mode"] == "hybrid"
    assert effective["seg_min_seconds"] == 8
    assert effective["lm_model"] == "request-model"
    assert "unrelated" not in effective
    assert settings.lm_model == "base-model"
    assert metadata["seg_min_seconds"] == 12
