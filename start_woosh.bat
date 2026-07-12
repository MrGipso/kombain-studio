@echo off
setlocal
rem ============================================================
rem  Pipeline Studio - optional Sony Research Woosh SFX server
rem
rem  Supply-chain policy:
rem    * this launcher never clones code or downloads model weights;
rem    * provision the locked environment and verify source/weights separately;
rem    * model weights are CC-BY-NC and are not for commercial use.
rem ============================================================

if /I not "%~1"=="--accept-noncommercial" (
    echo [BLOCKED] Woosh model weights are CC-BY-NC ^(non-commercial^).
    echo Review docs\THIRD_PARTY.md, then run explicitly:
    echo     start_woosh.bat --accept-noncommercial
    exit /b 2
)

cd /d "%~dp0Woosh"
if errorlevel 1 (
    echo [ERROR] The external Woosh directory was not found next to Pipeline Studio.
    echo Install a reviewed, pinned Woosh release manually; this launcher does not download it.
    exit /b 1
)

if not exist "pyproject.toml" (
    echo [ERROR] Woosh\pyproject.toml is missing. Refusing to run an unknown installation.
    exit /b 1
)
if not exist "uv.lock" (
    echo [ERROR] Woosh\uv.lock is missing. A locked dependency installation is required.
    exit /b 1
)

for %%M in (Woosh-DFlow TextConditionerA Woosh-AE) do (
    if not exist "checkpoints\%%M\config.yaml" (
        echo [ERROR] Missing checkpoints\%%M\config.yaml.
        echo Acquire and verify the model from the reviewed upstream release.
        exit /b 1
    )
    if not exist "checkpoints\%%M\weights.safetensors" (
        echo [ERROR] Missing checkpoints\%%M\weights.safetensors.
        echo Acquire and verify the model from the reviewed upstream release.
        exit /b 1
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] The pre-provisioned Woosh virtual environment is missing.
    echo Review the external lockfile, then provision it separately. This launcher
    echo never installs packages and never downloads code or model files.
    exit /b 1
)

rem Prevent known Hugging Face/Transformers loaders from filling a missing cache
rem during startup. A complete, reviewed installation succeeds offline; an
rem incomplete one fails closed instead of silently acquiring more artifacts.
set HF_HUB_OFFLINE=1
set TRANSFORMERS_OFFLINE=1
set HF_DATASETS_OFFLINE=1
set HF_HUB_DISABLE_TELEMETRY=1
set UV_OFFLINE=1

echo [WOOSH] Starting on http://127.0.0.1:8500 ^(Ctrl+C to stop^)
echo [WOOSH] Launcher acknowledgement recorded; release clearance is separate.
".venv\Scripts\python.exe" -m uvicorn api.api_server:app --host 127.0.0.1 --port 8500
exit /b %errorlevel%
