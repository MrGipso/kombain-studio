@echo off
cd /d "%~dp0"
title Pipeline Studio

rem Keep mutable runtime state outside the install/source tree. Preserve an
rem existing legacy settings file on the first launch after this migration.
if not defined PIPELINE_DATA_DIR set "PIPELINE_DATA_DIR=%LOCALAPPDATA%\PipelineStudio"
if not exist "%PIPELINE_DATA_DIR%" mkdir "%PIPELINE_DATA_DIR%"
if not defined PIPELINE_SETTINGS_PATH set "PIPELINE_SETTINGS_PATH=%PIPELINE_DATA_DIR%\settings.json"
if not defined PIPELINE_JOBS_DB set "PIPELINE_JOBS_DB=%PIPELINE_DATA_DIR%\jobs.sqlite3"
if not exist "%PIPELINE_SETTINGS_PATH%" if exist "content_pipeline_settings.json" copy /y "content_pipeline_settings.json" "%PIPELINE_SETTINGS_PATH%" >nul

echo ==================================================
echo   Pipeline Studio
echo ==================================================
echo.
echo [1/3] Building frontend...
cd frontend
call npm run build
if %errorlevel% neq 0 (
    echo.
    echo Frontend build FAILED. See the error above.
    pause
    exit /b %errorlevel%
)
cd ..

echo.
echo [2/3] Opening browser at http://localhost:8000 ...
rem Wait a few seconds for the server to come up, then open the default browser.
start "" /min cmd /c "timeout /t 5 >nul && explorer http://localhost:8000"

echo.
echo [3/3] Starting server. Keep this window open; close it to stop.
echo.
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000

echo.
echo Server stopped.
pause
