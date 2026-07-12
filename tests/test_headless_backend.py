from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_complete_web_backend_imports_without_desktop_gui_or_tkinter():
    """Keep the web server deployable on headless hosts.

    This catches indirect regressions such as router -> orchestration -> legacy
    desktop engine, which a simple grep of router imports can miss.
    """
    root = Path(__file__).resolve().parents[1]
    script = r"""
import builtins
import sys

real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "content_pipeline_gui" or name.startswith("tkinter"):
        raise AssertionError(f"web backend imported desktop dependency: {name}")
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded
from backend.main import app
schema = app.openapi()
assert schema["paths"]
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
