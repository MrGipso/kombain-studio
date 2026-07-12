from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


def test_pyside_sources_have_no_direct_legacy_desktop_imports():
    root = Path(__file__).resolve().parents[1]
    sources = [
        root / "ui" / "workers.py",
        root / "ui" / "tabs" / "scenes.py",
        root / "ui" / "tabs" / "shots.py",
        root / "content_pipeline_qt.py",
    ]
    imported: set[str] = set()
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

    assert "content_pipeline_gui" not in imported


@pytest.mark.skipif(
    importlib.util.find_spec("PySide6") is None,
    reason="optional desktop dependency PySide6 is not installed",
)
def test_pyside_workers_and_planning_tabs_do_not_load_tk_desktop_engine():
    root = Path(__file__).resolve().parents[1]
    script = r"""
import builtins
import sys
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "content_pipeline_gui" or name.startswith("tkinter"):
        raise AssertionError(f"PySide layer imported legacy Tk dependency: {name}")
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
import ui.workers
import ui.tabs.scenes
import ui.tabs.shots
import content_pipeline_qt
assert "content_pipeline_gui" not in sys.modules
assert "tkinter" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
