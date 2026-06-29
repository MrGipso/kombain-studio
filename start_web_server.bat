@echo off
cd /d "%~dp0"
echo Building frontend...
cd frontend
call npm run build
if %errorlevel% neq 0 (
    echo Frontend build failed.
    pause
    exit /b %errorlevel%
)
cd ..

echo Starting Pipeline Studio Web UI at http://localhost:8000
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000
pause
