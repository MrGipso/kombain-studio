@echo off
setlocal
rem ============================================================
rem  Kombain Studio - optional MOSS-SoundEffect v2 SFX server
rem  (Woosh-compatible shim; see tools\moss_sfx_server.py)
rem
rem  Supply-chain policy (same as start_woosh.bat):
rem    * this launcher never clones code and, by default, never downloads
rem      model weights; provision the environment separately;
rem    * model weights are Apache-2.0 (commercial use permitted);
rem    * pass --allow-download ONCE to let HuggingFace fetch the weights
rem      into its cache on first start.
rem ============================================================

set "ROOT=%~dp0"
set "MOSS_DIR=%ROOT%MOSS-TTS\moss_soundeffect_v2"
set "MOSS_PY=%ROOT%MOSS-TTS\.venv\Scripts\python.exe"

if not exist "%MOSS_DIR%\pyproject.toml" (
    echo [ERROR] MOSS-TTS\moss_soundeffect_v2 was not found next to Kombain Studio.
    echo Install a reviewed, pinned release manually; this launcher does not download it:
    echo     git clone https://github.com/OpenMOSS/MOSS-TTS.git
    echo See docs\MOSS_SFX.md for the full setup.
    pause
    exit /b 1
)

if not exist "%MOSS_PY%" (
    echo [ERROR] The pre-provisioned MOSS virtual environment is missing:
    echo     %MOSS_PY%
    echo Provision it separately ^(Python 3.12^); this launcher never installs packages:
    echo     py -3.12 -m venv MOSS-TTS\.venv
    echo     MOSS-TTS\.venv\Scripts\pip install --extra-index-url https://download.pytorch.org/whl/cu128 -e "MOSS-TTS\moss_soundeffect_v2[torch-cu128]"
    echo     MOSS-TTS\.venv\Scripts\pip install fastapi uvicorn soundfile
    pause
    exit /b 1
)

"%MOSS_PY%" -c "import fastapi, uvicorn, soundfile, moss_soundeffect_v2" 1>nul 2>nul
if errorlevel 1 (
    echo [ERROR] The MOSS environment is incomplete ^(need: moss_soundeffect_v2, fastapi, uvicorn, soundfile^).
    echo The actual import error:
    "%MOSS_PY%" -c "import fastapi, uvicorn, soundfile, moss_soundeffect_v2"
    echo See docs\MOSS_SFX.md for provisioning steps. This launcher never installs packages.
    pause
    exit /b 1
)

rem Fail closed offline unless the operator explicitly allows the one-time
rem weight download into the HuggingFace cache.
if /I "%~1"=="--allow-download" (
    echo [MOSS-SFX] Online mode: HuggingFace may download model weights now.
) else (
    set HF_HUB_OFFLINE=1
    set TRANSFORMERS_OFFLINE=1
    set HF_DATASETS_OFFLINE=1
)
set HF_HUB_DISABLE_TELEMETRY=1

rem The bundled MOSS scripts disable TorchDynamo to avoid Triton compile
rem failures on some setups; keep the same default here.
if not defined TORCHDYNAMO_DISABLE set TORCHDYNAMO_DISABLE=1

if not defined MOSS_SFX_PORT set MOSS_SFX_PORT=8510

echo [MOSS-SFX] Starting on http://127.0.0.1:%MOSS_SFX_PORT% ^(Ctrl+C to stop^)
echo [MOSS-SFX] Point woosh_url in Settings at this address to switch the SFX provider.
"%MOSS_PY%" "%ROOT%tools\moss_sfx_server.py"
exit /b %errorlevel%
