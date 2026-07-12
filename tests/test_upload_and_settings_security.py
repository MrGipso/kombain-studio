import asyncio
import io
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile

from backend.routers import settings as settings_router
from backend.routers import transcription
from core import config as config_module
from core.config import Settings
from core.project import create_project


def _upload(filename: str, content: bytes) -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO(content))


def test_audio_upload_rejects_executable_extension(tmp_path, monkeypatch):
    config = Settings(output_base=str(tmp_path))
    project = create_project("safe", tmp_path, settings=config)
    monkeypatch.setattr(transcription, "load_settings", lambda: config)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(transcription.upload_audio(project.dir.name, _upload("ffmpeg.exe", b"MZ")))

    assert caught.value.status_code == 400
    assert not (project.voice_dir / "ffmpeg.exe").exists()


def test_audio_upload_is_atomic_and_rejects_empty_files(tmp_path, monkeypatch):
    config = Settings(output_base=str(tmp_path))
    project = create_project("voice", tmp_path, settings=config)
    monkeypatch.setattr(transcription, "load_settings", lambda: config)

    result = asyncio.run(transcription.upload_audio(project.dir.name, _upload("voice.wav", b"RIFFdata")))
    assert Path(result["path"]).read_bytes() == b"RIFFdata"
    assert not list(project.voice_dir.glob(".upload-*.part"))

    with pytest.raises(HTTPException) as caught:
        asyncio.run(transcription.upload_audio(project.dir.name, _upload("empty.wav", b"")))
    assert caught.value.status_code == 400
    assert not (project.voice_dir / "empty.wav").exists()


def test_settings_reject_unknown_fields_and_project_hosted_executables(tmp_path, monkeypatch):
    project_root = tmp_path / "projects"
    project_root.mkdir()
    fake = project_root / "ffmpeg.exe"
    fake.write_bytes(b"not an executable")
    current = Settings(output_base=str(project_root))
    monkeypatch.setattr(settings_router, "load_settings", lambda: current)

    with pytest.raises(HTTPException) as unknown:
        asyncio.run(settings_router.update_settings({"ffmepg": str(fake)}))
    assert unknown.value.status_code == 422

    with pytest.raises(HTTPException) as unsafe:
        asyncio.run(settings_router.update_settings({"ffmpeg": str(fake)}))
    assert unsafe.value.status_code == 400


def test_settings_accept_read_only_secret_flags(tmp_path, monkeypatch):
    current = Settings(output_base=str(tmp_path), ffmpeg="")
    saved = []
    monkeypatch.setattr(settings_router, "load_settings", lambda: current)
    monkeypatch.setattr(settings_router, "save_settings", saved.append)
    monkeypatch.setattr(settings_router, "list_projects", lambda _path: [])

    result = asyncio.run(settings_router.update_settings({"pexels_api_key_set": True}))
    assert result == {"ok": True}
    assert len(saved) == 1


def test_settings_save_is_atomic_on_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"sentinel": true}', encoding="utf-8")

    def fail_replace(_source, _destination):
        raise OSError("disk failure")

    monkeypatch.setattr(config_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk failure"):
        config_module.save_settings(Settings(lm_model="changed"), path)

    assert path.read_text(encoding="utf-8") == '{"sentinel": true}'
    assert not list(tmp_path.glob(".settings.json.*.tmp"))
