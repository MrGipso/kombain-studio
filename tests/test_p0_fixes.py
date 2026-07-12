"""Regression tests for the P0 stop-ship fixes (audit follow-up)."""

import json
from types import SimpleNamespace

import pytest

from backend.routers import assets as assets_router
from core.export import card_route, card_source, VIDEO_ROUTES
from core.project import (
    ProjectExists,
    create_project,
    is_project_dir,
    list_projects,
)


# --- P0-3: reference route is a video route -------------------------------

def test_reference_route_recognized_from_route():
    assert card_route({"route": "reference"}) == "reference"
    assert "reference" in VIDEO_ROUTES


def test_reference_route_recognized_from_visual_type():
    assert card_route({"visual_type": "web_reference_card"}) == "reference"
    assert card_route({
        "route": "image",
        "visual_type": "web_reference_card",
    }) == "reference"


def test_reference_card_source_uses_video(tmp_path):
    video = tmp_path / "ref.mp4"
    video.write_bytes(b"x")
    image = tmp_path / "ref.png"
    image.write_bytes(b"x")
    card = {
        "route": "reference",
        "video_output": str(video),
        "image_output": str(image),
        "assigned_media": "",
    }
    # Must pick the finished reference VIDEO, not the still.
    assert card_source(card) == video


def test_route_resolver_accepts_dict_and_object():
    class Obj:
        route = "stock"
        visual_type = ""
        video_model = ""
    assert card_route(Obj()) == "stock"
    assert card_route({"route": "stock"}) == "stock"


def test_reference_failure_persists_kenburns_route_before_render(tmp_path, monkeypatch):
    """A failed fallback render must not leave a stale reference route on disk."""
    card = SimpleNamespace(
        route="reference",
        visual_type="web_reference_card",
        status="pending",
        notes="",
        image_model="",
        image_output="",
        video_output="",
        shot_index=1,
        start="00:00:00",
        end="00:00:04",
        approved=False,
    )
    settings = SimpleNamespace(
        comfy_url="http://127.0.0.1:8188",
        image_model_preference="Auto",
        ffmpeg="ffmpeg",
    )
    project = SimpleNamespace(settings=settings, metadata={})
    persisted: list[tuple[str, str]] = []

    monkeypatch.setattr(assets_router, "open_project", lambda *_args: project)
    monkeypatch.setattr(assets_router, "load_settings", lambda: settings)
    monkeypatch.setattr(assets_router, "_load_asset_board", lambda _path: [card])
    monkeypatch.setattr(assets_router, "_sync_asset_card_route", lambda *_args: card)
    monkeypatch.setattr(
        assets_router,
        "_write_asset_board",
        lambda _path, cards: persisted.append((cards[0].route, cards[0].visual_type)),
    )
    monkeypatch.setattr(
        assets_router.reference_lib,
        "fetch_reference_for_card",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        assets_router.asset_board,
        "preferred_realistic_image_model",
        lambda _preference: "krea",
    )
    monkeypatch.setattr(
        assets_router.comfy_generation,
        "generate_comfy_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("render failed")),
    )

    with pytest.raises(RuntimeError, match="render failed"):
        assets_router._generate_sync(tmp_path, 0, "route")

    assert persisted[-1] == ("kenburns", "still_kenburns")


# --- P0-6: unique project creation + real-project filter ------------------

def test_create_project_refuses_colliding_slug(tmp_path):
    p1 = create_project("My Video", tmp_path)
    with pytest.raises(ProjectExists):
        create_project("My Video", tmp_path)
    assert (p1.dir / ".project.json").is_file()
    assert not (tmp_path / "my-video-2").exists()


def test_list_projects_ignores_non_project_dirs(tmp_path):
    create_project("Real One", tmp_path)
    (tmp_path / ".uv-cache").mkdir()
    (tmp_path / "random_folder").mkdir()  # no manifest
    names = [p.name for p in list_projects(tmp_path)]
    assert "Real One" in names
    assert all(is_project_dir(tmp_path / p.dir.name) for p in list_projects(tmp_path))
    assert len(list_projects(tmp_path)) == 1


def test_is_project_dir_requires_manifest(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not is_project_dir(plain)
    (plain / ".project.json").write_text("{}", encoding="utf-8")
    assert is_project_dir(plain)


def test_is_project_dir_rejects_broken_manifest(tmp_path):
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".project.json").write_text("not-json", encoding="utf-8")
    assert not is_project_dir(broken)
