@echo off
cd /d "%~dp0"
title Kombain Studio (desktop)

rem Native-window launch. The server and the window share one process, so
rem closing the window stops the server. Runtime state paths are resolved by
rem launch.py itself and match the browser launchers.
rem
rem This file keeps a console window open, which is useful for diagnostics.
rem For everyday use start start_desktop.vbs instead: it runs this script
rem hidden, so only the application window remains on screen.

rem launch.py rebuilds the frontend itself when its sources changed (first run,
rem git pull, a newer release unpacked over this folder).

uv run --extra desktop-app python launch.py
if %errorlevel% neq 0 (
    echo.
    echo Startup failed. See the message above.
    rem Never pause when launched hidden from the .vbs: nobody could dismiss it.
    if not "%PIPELINE_DESKTOP_SILENT%"=="1" pause
    exit /b %errorlevel%
)
