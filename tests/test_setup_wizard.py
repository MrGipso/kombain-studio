import asyncio
import json
from pathlib import Path

from backend.routers import settings as settings_router
from backend.services import jobs
from core import setup_detect
from core.config import Settings, load_settings, save_settings


def _touch(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# --- setup_completed flag ---------------------------------------------------

def test_fresh_install_needs_setup(tmp_path):
    assert load_settings(tmp_path / "missing.json").setup_completed is False


def test_settings_file_from_before_the_wizard_counts_as_set_up(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"lm_model": "qwen"}), encoding="utf-8")
    assert load_settings(path).setup_completed is True


def test_explicit_flag_is_kept(tmp_path):
    path = tmp_path / "settings.json"
    save_settings(Settings(setup_completed=False), path)
    assert load_settings(path).setup_completed is False


def test_put_settings_can_finish_setup(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    save_settings(Settings(output_base=str(tmp_path / "projects"), setup_completed=False), settings_path)
    monkeypatch.setattr(settings_router, "load_settings", lambda: load_settings(settings_path))
    monkeypatch.setattr(settings_router, "save_settings", lambda value: save_settings(value, settings_path))
    monkeypatch.setenv("PIPELINE_JOBS_DB", str(tmp_path / "jobs.sqlite3"))
    jobs._initialized_db_path = None

    asyncio.run(settings_router.update_settings({"setup_completed": True}))

    assert load_settings(settings_path).setup_completed is True


# --- tool discovery -----------------------------------------------------------

def test_windows_finds_bundled_release_and_skips_vad_models(tmp_path, monkeypatch):
    root = tmp_path / "streamer"
    monkeypatch.setattr(setup_detect, "ROOT", root)
    cli = _touch(root / "wishper" / "whisper-cli.exe")
    _touch(root / "models" / "ggml-silero-v6.2.0.bin")
    small = _touch(root / "models" / "ggml-small.bin")
    turbo = _touch(root / "models" / "ggml-large-v3-turbo-q8_0.bin")
    program_files = tmp_path / "Program Files"
    ffmpeg = _touch(program_files / "ffmpeg" / "bin" / "ffmpeg.exe")

    found = setup_detect.detect(
        Settings(),
        platform="win32",
        environ={"ProgramFiles": str(program_files)},
        home=tmp_path / "home",
        which=lambda _name: None,
        probe_services=False,
    )

    assert found.platform == "windows"
    assert found.whisper_cli.candidates == [str(cli)]
    # Best model first; the VAD model is never offered for transcription.
    assert found.whisper_model.candidates == [str(turbo), str(small)]
    assert str(ffmpeg) in found.ffmpeg.candidates
    assert found.output_base.candidates == [str((root / "projects").resolve())]


def test_linux_finds_source_build_and_its_models(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_detect, "ROOT", tmp_path / "kombain-studio")
    home = tmp_path / "home"
    cli = _touch(home / "src" / "whisper.cpp" / "build" / "bin" / "whisper-cli")
    model = _touch(home / "src" / "whisper.cpp" / "models" / "ggml-base.bin")
    system_ffmpeg = _touch(tmp_path / "usr" / "bin" / "ffmpeg")

    found = setup_detect.detect(
        Settings(),
        platform="linux",
        environ={},
        home=home,
        which=lambda name: str(system_ffmpeg) if name == "ffmpeg" else None,
        probe_services=False,
    )

    assert found.whisper_cli.candidates == [str(cli)]
    assert found.whisper_model.candidates == [str(model)]
    assert found.ffmpeg.candidates[0] == str(system_ffmpeg)
    # Windows-only names are not offered on Linux.
    assert not any(c.endswith(".exe") for c in found.ffmpeg.candidates)


def test_current_values_are_reported_with_validity(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_detect, "ROOT", tmp_path)
    ffmpeg = _touch(tmp_path / "bin" / "ffmpeg")
    settings = Settings(
        ffmpeg=str(ffmpeg),
        whisper_cli=str(tmp_path / "gone" / "whisper-cli"),
        omnivoice_cli="omnivoice-infer",
    )

    found = setup_detect.detect(
        settings,
        platform="linux",
        environ={},
        home=tmp_path / "home",
        which=lambda name: "/opt/omnivoice-infer" if name == "omnivoice-infer" else None,
        probe_services=False,
    )

    assert found.ffmpeg.current_ok is True
    assert found.whisper_cli.current_ok is False
    # The default bare command name is valid when it resolves on PATH.
    assert found.omnivoice_cli.current_ok is True


# --- service probes -------------------------------------------------------------

def test_lm_probe_lists_models():
    calls = []

    def getter(url, timeout):
        calls.append(url)
        return True, {"data": [{"id": "qwen3-30b"}, {"id": "text-embedding-nomic"}]}, ""

    probe = setup_detect.probe_lm("http://127.0.0.1:1234/v1/", getter=getter)

    assert calls == ["http://127.0.0.1:1234/v1/models"]
    assert probe.reachable is True
    assert probe.models == ["qwen3-30b", "text-embedding-nomic"]


def test_cloud_lm_is_not_probed_without_its_key():
    def getter(url, timeout):  # pragma: no cover - must not be called
        raise AssertionError("cloud endpoint probed")

    probe = setup_detect.probe_lm("https://api.openai.com/v1", provider="openai_compatible", getter=getter)
    assert probe.reachable is False


def test_detect_probes_configured_and_default_urls(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_detect, "ROOT", tmp_path)
    seen = []
    monkeypatch.setattr(setup_detect, "probe_lm", lambda url, provider="lmstudio": seen.append(url) or setup_detect.ServiceProbe(url=url))
    monkeypatch.setattr(setup_detect, "probe_comfy", lambda url: setup_detect.ServiceProbe(url=url))

    found = setup_detect.detect(
        Settings(lm_url="http://192.168.1.5:1234/v1"),
        platform="linux", environ={}, home=tmp_path, which=lambda _n: None,
    )

    assert seen == ["http://192.168.1.5:1234/v1", "http://127.0.0.1:1234/v1"]
    assert [p.url for p in found.comfy] == ["http://127.0.0.1:8188"]


def test_setup_route_is_registered():
    from backend.main import app

    assert "/api/setup/detect" in app.openapi()["paths"]
