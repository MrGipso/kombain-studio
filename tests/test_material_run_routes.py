import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routers import assets as assets_router


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _project(root, *, stale=False):
    root.mkdir()
    _write(
        root / "01_transcript_segments.json",
        [{"start": "00:00:00", "end": "00:00:04", "text": "Локальный компьютер."}],
    )
    _write(
        root / "02_scenes.json",
        [{
            "index": 1,
            "start": "00:00:00",
            "end": "00:00:04",
            "title": "Локальный компьютер",
            "voice_text": "Локальный компьютер.",
            "visual_direction": "Компьютер на столе",
            "screen_text": "",
            "music_note": "",
            "retention_note": "",
        }],
    )
    _write(
        root / "04_shot_list.json",
        [{
            "index": 1,
            "scene_index": 1,
            "start": "00:00:00",
            "end": "00:00:04",
            "visual_type": "ai_image_then_i2v",
            "description": "Компьютер на столе",
            "asset_query": "computer on desk",
        }],
    )
    _write(
        root / "06_asset_board.json",
        [{
            "card_id": "card-1",
            "shot_index": 1,
            "scene_index": 1,
            "scene_title": "Локальный компьютер",
            "start": "00:00:00",
            "end": "00:00:04",
            "visual_type": "ai_image_then_i2v",
            "route": "image+video",
            "description": "Компьютер на столе",
            "asset_query": "computer on desk",
            "image_model": "krea2_turbo",
            "video_model": "ltx2.3_i2v",
            "remotion_template": "",
            "image_prompt": "",
            "video_prompt": "",
            "screen_text": "",
            "motion": "",
            "fallback": "",
            "status": "planned",
            "image_output": "",
            "video_output": "",
            "assigned_media": "",
        }],
    )
    status = "stale" if stale else "completed"
    _write(
        root / ".pipeline_manifest.json",
        {"stages": {"scenes": {"status": status}, "shots": {"status": status}}},
    )


def _client(monkeypatch, project_dir):
    app = FastAPI()
    app.include_router(assets_router.router, prefix="/api/projects")
    monkeypatch.setattr(assets_router, "_resolve_project", lambda _project_id: project_dir)
    return TestClient(app)


def test_material_preflight_and_background_start_contract(monkeypatch, tmp_path):
    project = tmp_path / "project"
    _project(project)
    monkeypatch.setattr(assets_router, "_start_material_run", lambda *_args: "job-123")
    monkeypatch.setattr(assets_router, "_comfy_available", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        assets_router,
        "_material_workflow_preflight",
        lambda *_args, **_kwargs: {"ok": True},
    )

    with _client(monkeypatch, project) as client:
        preflight = client.get("/api/projects/example/materials/preflight")
        initial = client.get("/api/projects/example/materials/run")
        started = client.post("/api/projects/example/materials/run", json={})

    assert preflight.status_code == 200
    assert preflight.json()["allowed"] is True
    assert preflight.json()["pending_images"] == 1
    assert preflight.json()["pending_videos"] == 1
    assert preflight.json()["required_services"] == ["comfy"]
    assert initial.json()["status"] == "not_started"
    assert started.status_code == 202
    assert started.json()["job_id"] == "job-123"
    assert started.json()["policy"]["max_image_attempts"] == 2
    assert started.json()["policy"]["max_video_attempts"] == 2


def test_provider_preflight_checks_only_required_material_workflows(
    monkeypatch, tmp_path
):
    project = tmp_path / "project"
    _project(project)
    settings = SimpleNamespace(
        comfy_url="http://127.0.0.1:8188",
        comfy_krea_workflow="",
        comfy_ideogram_workflow="",
        comfy_ltx_workflow="",
        comfy_ltx_draft_workflow="",
        comfy_ace_workflow="",
        ltx_draft_mode="off",
    )
    paths = assets_router.comfy_client.configured_workflow_paths(settings)
    object_info = {}
    for name in ("krea", "ltx"):
        workflow = json.loads(paths[name].read_text("utf-8"))
        for node in workflow.values():
            object_info[str(node["class_type"])] = {}
    monkeypatch.setattr(
        assets_router.comfy_client,
        "comfy_request_json",
        lambda *_args, **_kwargs: object_info,
    )

    result = assets_router._material_workflow_preflight(project, settings)

    assert result["ok"] is True
    assert result["required_workflows"] == ["krea", "ltx"]
    assert set(result["workflows"]) == {"krea", "ltx"}


def test_provider_preflight_includes_enabled_reference_workflow(
    monkeypatch, tmp_path
):
    project = tmp_path / "project"
    _project(project)
    board_path = project / "06_asset_board.json"
    cards = json.loads(board_path.read_text("utf-8"))
    cards[0]["reference_frame_id"] = "office"
    cards.append(
        {
            **cards[0],
            "card_id": "card-2",
            "shot_index": 2,
            "start": "00:00:04",
            "end": "00:00:08",
        }
    )
    _write(board_path, cards)
    reference = tmp_path / "reference.json"
    _write(
        reference,
        {
            "load": {
                "class_type": "LoadImage",
                "inputs": {"image": "old.png"},
                "_meta": {"title": "ENGINE_REFERENCE_IMAGE"},
            },
            "prompt": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "old"},
                "_meta": {"title": "ENGINE_PROMPT"},
            },
            "save": {
                "class_type": "SaveImage",
                "inputs": {"filename_prefix": "old"},
                "_meta": {"title": "ENGINE_OUTPUT"},
            },
        },
    )
    settings = SimpleNamespace(
        comfy_url="http://127.0.0.1:8188",
        comfy_krea_workflow="",
        comfy_reference_workflow=str(reference),
        reference_conditioning_mode="required",
        comfy_ideogram_workflow="",
        comfy_ltx_workflow="",
        comfy_ltx_draft_workflow="",
        comfy_ace_workflow="",
    )
    paths = assets_router.comfy_client.configured_workflow_paths(settings)
    object_info = {}
    for name in ("krea", "ltx", "reference"):
        workflow = json.loads(paths[name].read_text("utf-8"))
        for node in workflow.values():
            object_info[str(node["class_type"])] = {}
    monkeypatch.setattr(
        assets_router.comfy_client,
        "comfy_request_json",
        lambda *_args, **_kwargs: object_info,
    )

    result = assets_router._material_workflow_preflight(project, settings)

    assert result["ok"] is True
    assert result["required_workflows"] == ["krea", "ltx", "reference"]


def test_unattended_preflight_uses_full_ltx_when_manual_draft_mode_is_auto(
    monkeypatch, tmp_path
):
    project = tmp_path / "project"
    _project(project)
    settings = SimpleNamespace(
        comfy_url="http://127.0.0.1:8188",
        comfy_krea_workflow="",
        comfy_ideogram_workflow="",
        comfy_ltx_workflow="",
        comfy_ltx_draft_workflow="",
        comfy_ace_workflow="",
        ltx_draft_mode="auto",
    )
    paths = assets_router.comfy_client.configured_workflow_paths(settings)
    object_info = {}
    for name in ("krea", "ltx"):
        workflow = json.loads(paths[name].read_text("utf-8"))
        for node in workflow.values():
            object_info[str(node["class_type"])] = {}
    monkeypatch.setattr(
        assets_router.comfy_client,
        "comfy_request_json",
        lambda *_args, **_kwargs: object_info,
    )

    result = assets_router._material_workflow_preflight(project, settings)

    assert result["ok"] is True
    assert result["required_workflows"] == ["krea", "ltx"]
    assert "ltx_draft" not in result["workflows"]


def test_stale_plan_blocks_material_run_before_any_provider_call(monkeypatch, tmp_path):
    project = tmp_path / "project"
    _project(project, stale=True)
    monkeypatch.setattr(
        assets_router,
        "_start_material_run",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not start")),
    )

    with _client(monkeypatch, project) as client:
        response = client.post("/api/projects/example/materials/run", json={})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["allowed"] is False
    assert {issue["code"] for issue in detail["issues"]} == {"stale_scenes", "stale_shots"}


def test_unavailable_comfy_blocks_run_instead_of_fallbacking_entire_queue(
    monkeypatch, tmp_path
):
    project = tmp_path / "project"
    _project(project)
    monkeypatch.setattr(assets_router, "_comfy_available", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        assets_router,
        "_start_material_run",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not start")),
    )

    with _client(monkeypatch, project) as client:
        response = client.post("/api/projects/example/materials/run", json={})

    assert response.status_code == 503
    assert response.json()["detail"]["service"] == "comfy"


def test_incompatible_material_workflow_blocks_run_before_job_creation(
    monkeypatch, tmp_path
):
    project = tmp_path / "project"
    _project(project)
    monkeypatch.setattr(assets_router, "_comfy_available", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        assets_router,
        "_material_workflow_preflight",
        lambda *_args, **_kwargs: {
            "ok": False,
            "message": "Требуемый workflow несовместим.",
            "required_workflows": ["krea"],
            "workflows": {
                "krea": {"ok": False, "missing_node_types": ["MissingNode"]}
            },
        },
    )
    monkeypatch.setattr(
        assets_router,
        "_start_material_run",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not start")),
    )

    with _client(monkeypatch, project) as client:
        response = client.post("/api/projects/example/materials/run", json={})

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["workflow_preflight"]["required_workflows"] == ["krea"]
