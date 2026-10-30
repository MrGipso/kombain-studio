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

    assert calls["count"] == len(files._REPLACE_RETRY_DELAYS) + 1


def test_replace_does_not_retry_permission_errors_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "_RETRY_SHARING_VIOLATIONS", False)
    calls = _flaky_replace(monkeypatch, failures=1)

    with pytest.raises(PermissionError):
        files.replace_with_retry(tmp_path / "a", tmp_path / "b")

    assert calls["count"] == 1
