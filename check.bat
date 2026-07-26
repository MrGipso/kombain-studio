@echo off
rem ============================================================
rem  Kombain Studio - pre-commit checks (run before every commit)
rem    1) Lockfile check
rem    2) Deterministic release SBOM
rem    3) Python tests
rem    4) Frontend lint
rem    5) Production frontend build
rem  Add new checks here so they run as one habit-forming command.
rem ============================================================
setlocal
cd /d "%~dp0"
set FAILED=0

echo [1/5] uv lock --check...
uv lock --check
if errorlevel 1 set FAILED=1

echo.
echo [2/5] deterministic CycloneDX SBOM...
uv run --locked python tools\generate_sbom.py --output artifacts\pipeline-studio.cdx.json
if errorlevel 1 set FAILED=1

echo.
echo [3/5] pytest...
uv run --extra dev pytest tests -q
if errorlevel 1 set FAILED=1

echo.
echo [4/5] frontend lint...
pushd frontend
call npm run lint
if errorlevel 1 set FAILED=1

echo.
echo [5/5] frontend production build...
call npm run build
if errorlevel 1 set FAILED=1
popd

echo.
if "%FAILED%"=="1" (
    echo ============ CHECKS FAILED ============
    exit /b 1
) else (
    echo ============ ALL CHECKS PASSED ============
)
endlocal
