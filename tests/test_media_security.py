import asyncio
import json

import pytest
from fastapi import HTTPException

from backend.routers import media
from core.config import Settings
from core.project import create_project


def test_media_endpoint_only_serves_media_inside_real_projects(tmp_path, monkeypatch):
    settings = Settings(output_base=str(tmp_path))
    project = create_project("real", tmp_path, settings=settings)
    video = project.assets_dir / "videos" / "clip.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(media, "load_settings", lambda: settings)

    response = asyncio.run(media.serve_media(str(video)))
    assert response.path == str(video.resolve())
    assert response.media_type == "video/mp4"


def test_media_endpoint_rejects_project_metadata_and_cache_files(tmp_path, monkeypatch):
    settings = Settings(output_base=str(tmp_path))
    project = create_project("real", tmp_path, settings=settings)
    metadata = project.dir / "secret.json"
    metadata.write_text(json.dumps({"secret": True}), encoding="utf-8")
    cache = tmp_path / ".uv-cache"
    cache.mkdir()
    fake_video = cache / "secret.mp4"
    fake_video.write_bytes(b"not project media")
    monkeypatch.setattr(media, "load_settings", lambda: settings)

    for candidate in (metadata, fake_video):
        with pytest.raises(HTTPException) as caught:
            asyncio.run(media.serve_media(str(candidate)))
        assert caught.value.status_code == 403
