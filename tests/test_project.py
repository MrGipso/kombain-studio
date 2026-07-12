from pathlib import Path

import pytest

from core.project import safe_project_path, is_within


def test_safe_project_path_valid(tmp_path):
    (tmp_path / "komar").mkdir()
    assert safe_project_path("komar", tmp_path) == (tmp_path / "komar").resolve()
    # nested relative path stays inside the base
    (tmp_path / "komar" / "sub").mkdir()
    assert safe_project_path("komar/sub", tmp_path) == (tmp_path / "komar" / "sub").resolve()


@pytest.mark.parametrize(
    "bad",
    ["../etc", "..", "/etc/passwd", "C:\\Windows\\x", "c:/windows", "a\\b", "", "   "],
)
def test_safe_project_path_rejects_traversal(bad, tmp_path):
    with pytest.raises(ValueError):
        safe_project_path(bad, tmp_path)


def test_is_within(tmp_path):
    assert is_within(tmp_path / "a" / "b.mp4", tmp_path) is True
    assert is_within(tmp_path, tmp_path) is True
    assert is_within(Path("/etc/passwd"), tmp_path) is False
