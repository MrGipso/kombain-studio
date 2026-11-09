# PyInstaller spec for the installed desktop app (onedir).
#
#   uv sync --locked --extra desktop-app --extra local-ai --extra bundle
#   (cd frontend && npm ci && npm run build)
#   uv run pyinstaller packaging/kombain-studio.spec --noconfirm
#
# Result: dist/KombainStudio/KombainStudio.exe plus _internal/. The Windows
# installer (packaging/installer.iss) packs that folder. The same spec builds
# on Linux, which is how the frozen app is tested without Windows.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent
WINDOWS = sys.platform == "win32"

static = ROOT / "backend" / "static" / "index.html"
if not static.is_file():
    raise SystemExit("Build the frontend first: cd frontend && npm ci && npm run build")

datas = [
    (str(ROOT / "backend" / "static"), "backend/static"),
    (str(ROOT / "workflows"), "workflows"),
    (str(ROOT / "presets"), "presets"),
    # Run as a script by the external OmniVoice Python, never imported here.
    (str(ROOT / "tools" / "omnivoice_batch.py"), "tools"),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "docs" / "THIRD_PARTY.md"), "docs"),
]

hiddenimports = (
    collect_submodules("backend")
    + collect_submodules("core")
    # uvicorn picks its loop/protocol implementations by name at runtime.
    + collect_submodules("uvicorn")
    + ["lmstudio"]
)

excludes = [
    "tkinter", "pytest", "_pytest", "playwright", "PyInstaller",
    "tools.self_update", "tools.frontend_build", "tools.golden_e2e",
    "tools.local_model_eval", "tools.generate_sbom", "tools.omnivoice_batch",
]

a = Analysis(
    [str(ROOT / "launch.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="KombainStudio",
    icon=str(ROOT / "packaging" / "kombain-studio.ico"),
    # A window app on Windows: no console behind it. Kept on Linux, where the
    # build is only used for testing.
    console=not WINDOWS,
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, name="KombainStudio", upx=False)
