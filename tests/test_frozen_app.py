"""The installed (PyInstaller) app: where it writes and how it updates."""

import sys
from pathlib import Path

import launch
from core import updates


def test_workspace_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_WORKSPACE_DIR", str(tmp_path / "ws"))
    assert launch.frozen_workspace() == tmp_path / "ws"
    assert (tmp_path / "ws").is_dir()


def test_workspace_defaults_to_documents(tmp_path, monkeypatch):
    monkeypatch.delenv("PIPELINE_WORKSPACE_DIR", raising=False)
    monkeypatch.setattr(launch, "_documents_dir", lambda: tmp_path / "Documents")
    assert launch.frozen_workspace() == tmp_path / "Documents" / "Комбайн Studio"


def test_window_app_without_streams_logs_to_a_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "platform", "linux")  # leave subprocess alone here
    log_path = launch.configure_frozen_process(tmp_path)
    print("hello from the app")
    sys.stdout.flush()
    assert log_path == tmp_path / "logs" / "app.log"
    assert "hello from the app" in log_path.read_text(encoding="utf-8")
    sys.stdout.close()


def test_installed_app_is_told_to_run_the_new_installer(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    (tmp_path / ".git").mkdir()  # even a stray .git must not switch it to git updates
    assert updates._install_kind(tmp_path) == "installer"


def test_bundle_spec_ships_the_runtime_resources():
    spec = (Path(launch.ROOT) / "packaging" / "kombain-studio.spec").read_text(encoding="utf-8")
    for resource in ('"backend" / "static"', '"workflows"', '"presets"', '"omnivoice_batch.py"'):
        assert resource in spec
