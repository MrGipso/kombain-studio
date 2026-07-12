from __future__ import annotations

import asyncio
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

import content_pipeline_gui as cpg
from backend.routers import assets as assets_router
from core import asset_variants
from core.config import Settings
from core.models import AssetCard
from core.project import create_project


def _card(**overrides) -> AssetCard:
    values = {
        "shot_index": 1,
        "scene_index": 1,
        "scene_title": "Scene",
        "start": "00:00:00",
        "end": "00:00:05",
        "visual_type": "ai_image",
        "route": "image",
        "description": "A server room",
        "asset_query": "server room",
        "image_model": "krea2",
        "video_model": "",
        "remotion_template": "",
        "image_prompt": "Wide server room",
        "video_prompt": "",
        "screen_text": "",
        "motion": "static",
        "fallback": "still",
        "image_output": "",
        "video_output": "",
        "remotion_output": "",
        "status": "planned",
        "notes": "",
        "approved": False,
    }
    values.update(overrides)
    return AssetCard(**values)


def _client_for(project, monkeypatch) -> TestClient:
    monkeypatch.setattr(assets_router, "_resolve_project", lambda _project_id: project.dir)
    app = FastAPI()
    app.include_router(assets_router.router, prefix="/projects")
    return TestClient(app)


def _project_with_board(tmp_path, monkeypatch, card: AssetCard | None = None):
    settings = Settings(output_base=str(tmp_path))
    project = create_project("Board", tmp_path, settings=settings)
    cpg.write_asset_board(project.dir, [card or _card()])
    return project, _client_for(project, monkeypatch)


def test_bulk_save_uses_strong_etag_and_rejects_stale_or_missing_revision(
    tmp_path, monkeypatch
):
    project, client = _project_with_board(tmp_path, monkeypatch)
    response = client.get(f"/projects/{project.dir.name}/asset-board")
    assert response.status_code == 200
    original_etag = response.headers["etag"]
    assert original_etag.startswith('"') and original_etag.endswith('"')
    assert len(original_etag) == 66  # quotes + full SHA-256

    payload = response.json()
    payload[0]["description"] = "First editor won"
    saved = client.post(
        f"/projects/{project.dir.name}/asset-board/save",
        headers={"If-Match": original_etag},
        json=payload,
    )
    assert saved.status_code == 200
    current_etag = saved.headers["etag"]
    assert current_etag != original_etag
    assert saved.json()["rev"] == current_etag.strip('"')

    stale_payload = payload.copy()
    stale_payload[0] = {**payload[0], "description": "Stale editor overwrote it"}
    stale = client.post(
        f"/projects/{project.dir.name}/asset-board/save",
        headers={"If-Match": original_etag},
        json=stale_payload,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["current"] == current_etag.strip('"')
    assert cpg.load_asset_board(project.dir)[0].description == "First editor won"

    missing = client.post(
        f"/projects/{project.dir.name}/asset-board/save",
        json=payload,
    )
    assert missing.status_code == 428

    # Compatibility with the unquoted revision token returned in the JSON body.
    payload[0]["notes"] = "legacy token accepted"
    legacy = client.post(
        f"/projects/{project.dir.name}/asset-board/save",
        headers={"If-Match": saved.json()["rev"]},
        json=payload,
    )
    assert legacy.status_code == 200


def test_user_board_mutations_are_rejected_while_project_job_runs(tmp_path, monkeypatch):
    project, client = _project_with_board(tmp_path, monkeypatch)
    board = client.get(f"/projects/{project.dir.name}/asset-board")
    monkeypatch.setenv("PIPELINE_JOBS_DB", str(tmp_path / "jobs.sqlite3"))

    with assets_router.jobs.project_job(project.dir.name, "Рендер кадра"):
        bulk = client.post(
            f"/projects/{project.dir.name}/asset-board/save",
            headers={"If-Match": board.headers["etag"]},
            json=board.json(),
        )
        assert bulk.status_code == 409
        assert "Рендер кадра" in bulk.json()["detail"]

        single = client.put(
            f"/projects/{project.dir.name}/asset-board/0",
            json={"description": "racing edit"},
        )
        assert single.status_code == 409

        # The render worker already owns the long-job lock and writes directly;
        # it must not attempt to re-enter the short HTTP mutation guard.
        worker_cards = cpg.load_asset_board(project.dir)
        worker_cards[0].notes = "written by active worker"
        cpg.write_asset_board(project.dir, worker_cards)

    stored = cpg.load_asset_board(project.dir)[0]
    assert stored.description == "A server room"
    assert stored.notes == "written by active worker"


def test_user_media_paths_cannot_escape_project_or_be_snapshotted(tmp_path, monkeypatch):
    project, client = _project_with_board(tmp_path, monkeypatch)
    secret = tmp_path.parent / "outside-secret.mp4"
    secret.write_bytes(b"not really a video, but confidential")

    rejected = client.put(
        f"/projects/{project.dir.name}/asset-board/0",
        json={"assigned_media": str(secret)},
    )
    assert rejected.status_code == 403
    detail = rejected.json()["detail"]
    assert detail["violations"] == [{"card_index": 0, "field": "assigned_media"}]
    assert cpg.load_asset_board(project.dir)[0].assigned_media == ""
    assert asset_variants.list_variants(project.dir) == []

    board = client.get(f"/projects/{project.dir.name}/asset-board")
    payload = board.json()
    payload[0]["video_output"] = str(secret)
    rejected_bulk = client.post(
        f"/projects/{project.dir.name}/asset-board/save",
        headers={"If-Match": board.headers["etag"]},
        json=payload,
    )
    assert rejected_bulk.status_code == 403
    assert asset_variants.list_variants(project.dir) == []


def test_project_local_media_is_accepted_and_update_returns_new_etag(tmp_path, monkeypatch):
    project, client = _project_with_board(tmp_path, monkeypatch)
    media = project.assets_images_dir / "manual.png"
    media.write_bytes(b"png-ish")
    old_etag = client.get(f"/projects/{project.dir.name}/asset-board").headers["etag"]

    updated = client.put(
        f"/projects/{project.dir.name}/asset-board/0",
        json={"assigned_media": str(media)},
    )
    assert updated.status_code == 200
    assert updated.headers["etag"] != old_etag
    variants = asset_variants.list_variants(project.dir, card_index=0)
    assert len(variants) == 1
    assert variants[0]["output_field"] == "assigned_media"


def test_card_id_cannot_be_replaced_by_patch_or_bulk_save(tmp_path, monkeypatch):
    project, client = _project_with_board(tmp_path, monkeypatch)
    board = client.get(f"/projects/{project.dir.name}/asset-board")

    patched = client.put(
        f"/projects/{project.dir.name}/asset-board/0",
        json={"card_id": "replacement-id"},
    )
    assert patched.status_code == 409

    payload = board.json()
    payload[0]["card_id"] = "replacement-id"
    bulk = client.post(
        f"/projects/{project.dir.name}/asset-board/save",
        headers={"If-Match": board.headers["etag"]},
        json=payload,
    )
    assert bulk.status_code == 409
    assert cpg.load_asset_board(project.dir)[0].card_id != "replacement-id"


def test_long_card_actions_require_current_revision_and_stable_card_id(
    tmp_path,
    monkeypatch,
):
    project, client = _project_with_board(tmp_path, monkeypatch)
    monkeypatch.setenv("PIPELINE_JOBS_DB", str(tmp_path / "jobs.sqlite3"))
    assets_router.jobs._initialized_db_path = None
    board = client.get(f"/projects/{project.dir.name}/asset-board")
    etag = board.headers["etag"]
    card_id = board.json()[0]["card_id"]

    missing = client.post(f"/projects/{project.dir.name}/generate/0")
    assert missing.status_code == 428

    wrong_card = client.post(
        f"/projects/{project.dir.name}/generate/0",
        headers={"If-Match": etag, "X-Card-ID": "another-card"},
    )
    assert wrong_card.status_code == 409
    assert wrong_card.json()["detail"]["current_card_id"] == card_id

    changed = client.put(
        f"/projects/{project.dir.name}/asset-board/0",
        headers={"If-Match": etag},
        json={"description": "new revision"},
    )
    assert changed.status_code == 200
    stale = client.post(
        f"/projects/{project.dir.name}/asset-board/0/stock",
        headers={"If-Match": etag, "X-Card-ID": card_id},
    )
    assert stale.status_code == 409


def test_unchanged_legacy_external_media_path_remains_editable(tmp_path, monkeypatch):
    legacy = tmp_path.parent / "legacy-external.mp4"
    legacy.write_bytes(b"legacy")
    project, client = _project_with_board(
        tmp_path,
        monkeypatch,
        _card(assigned_media=str(legacy)),
    )
    board = client.get(f"/projects/{project.dir.name}/asset-board")
    payload = board.json()
    payload[0]["description"] = "Metadata may still be edited"

    saved = client.post(
        f"/projects/{project.dir.name}/asset-board/save",
        headers={"If-Match": board.headers["etag"]},
        json=payload,
    )
    assert saved.status_code == 200
    assert cpg.load_asset_board(project.dir)[0].assigned_media == str(legacy)
    assert asset_variants.list_variants(project.dir) == []


def test_rebuild_holds_project_job_through_record_and_write(tmp_path, monkeypatch):
    project, _client = _project_with_board(tmp_path, monkeypatch)
    previous = cpg.load_asset_board(project.dir)
    rebuilt = [_card(description="rebuilt")]
    events: list[str] = []

    @contextmanager
    def fake_project_job(*_args, **_kwargs):
        events.append("enter")
        yield object()
        events.append("exit")

    monkeypatch.setattr(assets_router.jobs, "project_job", fake_project_job)
    monkeypatch.setattr(assets_router.jobs, "check", lambda _event: None)
    monkeypatch.setattr(
        assets_router,
        "_load_asset_board",
        lambda _path: events.append("load") or previous,
    )
    monkeypatch.setattr(
        assets_router,
        "_build_asset_board",
        lambda _path: events.append("build") or rebuilt,
    )
    monkeypatch.setattr(
        assets_router,
        "_record_manual_changes",
        lambda *_args, **_kwargs: events.append("record"),
    )
    monkeypatch.setattr(
        assets_router,
        "_write_asset_board",
        lambda *_args, **_kwargs: events.append("write"),
    )

    result = asyncio.run(assets_router.rebuild_asset_board(project.dir.name))
    assert result["ok"] is True
    assert events == ["enter", "load", "build", "record", "write", "exit"]
