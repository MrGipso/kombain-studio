from __future__ import annotations

import json
import tomllib
from pathlib import Path

from core.version import __version__


ROOT = Path(__file__).resolve().parents[1]


def test_release_candidate_version_is_consistent_across_surfaces():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    frontend = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    frontend_lock = json.loads(
        (ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8")
    )

    assert __version__ == "1.0.0-rc.2"
    assert pyproject["project"]["version"] == "1.0.0rc2"
    assert frontend["version"] == __version__
    assert frontend_lock["version"] == __version__
    assert frontend_lock["packages"][""]["version"] == __version__
