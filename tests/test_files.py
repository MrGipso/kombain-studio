import os

import pytest

from core import files


def _flaky_replace(monkeypatch, failures: int):
    original = os.replace
    calls = {"count": 0}

    def replace(source, destination):
        calls["count"] += 1
        if calls["count"] <= failures:
            raise PermissionError(13, "The process cannot access the file")
        original(source, destination)

    monkeypatch.setattr(files.os, "replace", replace)
    monkeypatch.setattr(files.time, "sleep", lambda _seconds: None)
    return calls


def test_replace_waits_out_a_windows_sharing_violation(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "_RETRY_SHARING_VIOLATIONS", True)
    calls = _flaky_replace(monkeypatch, failures=2)
    source = tmp_path / "board.json.tmp"
    destination = tmp_path / "board.json"
    source.write_text("new", encoding="utf-8")
    destination.write_text("old", encoding="utf-8")

    files.replace_with_retry(source, destination)

    assert calls["count"] == 3
    assert destination.read_text(encoding="utf-8") == "new"
    assert not source.exists()


def test_replace_gives_up_after_the_retry_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "_RETRY_SHARING_VIOLATIONS", True)
    calls = _flaky_replace(monkeypatch, failures=100)

    with pytest.raises(PermissionError):
        files.replace_with_retry(tmp_path / "a", tmp_path / "b")

    assert calls["count"] == len(files._SHARING_RETRY_DELAYS) + 1


def test_replace_does_not_retry_permission_errors_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "_RETRY_SHARING_VIOLATIONS", False)
    calls = _flaky_replace(monkeypatch, failures=1)

    with pytest.raises(PermissionError):
        files.replace_with_retry(tmp_path / "a", tmp_path / "b")

    assert calls["count"] == 1


def test_corrupt_settings_are_preserved_before_defaults_are_used(tmp_path):
    from core.config import Settings, load_settings

    path = tmp_path / "settings.json"
    original = b'{"ffmpeg": "C:/tools/ffmpeg.exe", '
    path.write_bytes(original)

    assert load_settings(path) == Settings()
    load_settings(path)

    copies = list((tmp_path / ".corrupt").iterdir())
    assert len(copies) == 1
    assert copies[0].name.startswith("settings.json.")
    assert copies[0].read_bytes() == original


def test_non_object_settings_json_counts_as_corrupt(tmp_path):
    from core.config import Settings, load_settings

    path = tmp_path / "settings.json"
    path.write_text("[]", encoding="utf-8")

    assert load_settings(path) == Settings()
    assert len(list((tmp_path / ".corrupt").iterdir())) == 1


def test_unreadable_settings_raise_instead_of_becoming_defaults(tmp_path, monkeypatch):
    # The settings route saves loaded values plus a patch; silently loading
    # defaults here would wipe every configured path and key on the next save.
    from core import config

    path = tmp_path / "settings.json"
    path.write_text('{"ffmpeg": "x"}', encoding="utf-8")

    def locked(*_args, **_kwargs):
        raise OSError("device not ready")

    monkeypatch.setattr(config, "read_text_with_retry", locked)

    with pytest.raises(OSError, match="device not ready"):
        config.load_settings(path)


def test_read_text_waits_out_a_windows_sharing_violation(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "_RETRY_SHARING_VIOLATIONS", True)
    monkeypatch.setattr(files.time, "sleep", lambda _seconds: None)
    path = tmp_path / "board.json"
    path.write_text("[]", encoding="utf-8")
    original = type(path).read_text
    calls = {"count": 0}

    def flaky_read_text(self, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise PermissionError(13, "The process cannot access the file")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "read_text", flaky_read_text)

    assert files.read_text_with_retry(path) == "[]"
    assert calls["count"] == 2


def test_project_read_json_preserves_corrupt_artifact(tmp_path):
    from core.project import Project

    project = Project(name="demo", dir=tmp_path)
    path = project.file_path("scenes")
    path.write_text('[{"index": 1,', encoding="utf-8")

    assert project.read_json("scenes") is None

    copies = list((tmp_path / ".corrupt").iterdir())
    assert len(copies) == 1
    assert copies[0].read_text(encoding="utf-8") == '[{"index": 1,'
