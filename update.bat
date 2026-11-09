@echo off
setlocal
cd /d "%~dp0"
title Kombain Studio - update

rem Updates a git checkout: pulls the latest code of the current branch,
rem syncs Python packages and rebuilds the web UI if its sources changed.
rem Settings and job history live in %%LOCALAPPDATA%%\PipelineStudio and are
rem not touched. Close the application before running this.

where uv >nul 2>nul
if errorlevel 1 (
    echo uv not found. Install it: https://docs.astral.sh/uv/
    pause
    exit /b 1
)

uv run --no-sync python tools\self_update.py
if errorlevel 1 (
    pause
    exit /b 1
)

echo.
echo Updating Python packages...
uv sync --inexact --extra desktop-app
if errorlevel 1 (
    echo Python packages could not be updated. See the error above.
    pause
    exit /b 1
)

echo.
uv run python tools\frontend_build.py --if-stale
if errorlevel 1 (
    echo Frontend build FAILED. See the error above.
    pause
    exit /b 1
)

echo.
echo Done. Start Kombain Studio as usual.
pause
