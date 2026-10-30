from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_complete_web_backend_imports_without_gui_toolkits():
    """Keep the web server deployable on headless hosts.

    This catches indirect regressions such as router -> orchestration -> GUI
    toolkit, which a simple grep of router imports can miss.
    """
    root = Path(__file__).resolve().parents[1]
    script = r"""
import builtins
import sys

real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.startswith(("tkinter", "PySide6")):
        raise AssertionError(f"web backend imported GUI toolkit: {name}")
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded
from backend.main import app
schema = app.openapi()
assert schema["paths"]
assert "tkinter" not in sys.modules
assert "PySide6" not in sys.modules
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
