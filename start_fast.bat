@echo off
cd /d "%~dp0"
title Kombain Studio (fast)

if not defined PIPELINE_DATA_DIR set "PIPELINE_DATA_DIR=%LOCALAPPDATA%\PipelineStudio"
if not exist "%PIPELINE_DATA_DIR%" mkdir "%PIPELINE_DATA_DIR%"
if not defined PIPELINE_SETTINGS_PATH set "PIPELINE_SETTINGS_PATH=%PIPELINE_DATA_DIR%\settings.json"
if not defined PIPELINE_JOBS_DB set "PIPELINE_JOBS_DB=%PIPELINE_DATA_DIR%\jobs.sqlite3"
if not exist "%PIPELINE_SETTINGS_PATH%" if exist "content_pipeline_settings.json" copy /y "content_pipeline_settings.json" "%PIPELINE_SETTINGS_PATH%" >nul

rem Fast launch: rebuilds the frontend only when its sources changed since the
rem last build (first run, git pull, a newer release unpacked over this folder).
uv run python tools\frontend_build.py --if-stale
if %errorlevel% neq 0 (
    echo Frontend build FAILED.
    pause
    exit /b %errorlevel%
)

echo Opening browser at http://localhost:8000 ...
start "" /min cmd /c "timeout /t 4 >nul && explorer http://localhost:8000"

echo Starting server. Keep this window open; close it to stop.
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000

echo Server stopped.
pause
