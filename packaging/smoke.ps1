# Start the built app without a window (--smoke) and fail if it does not
# serve its API and interface. The app has no console, so its output goes to
# the log in the data folder, printed here.
param([Parameter(Mandatory = $true)][string]$Exe)

$root = Join-Path $env:RUNNER_TEMP ("smoke-" + [guid]::NewGuid())
$env:PIPELINE_DATA_DIR = Join-Path $root "data"
$env:PIPELINE_WORKSPACE_DIR = Join-Path $root "workspace"
$env:PIPELINE_PORT = "8765"

$p = Start-Process $Exe -ArgumentList "--smoke" -Wait -PassThru
$log = Join-Path $env:PIPELINE_DATA_DIR "logs\app.log"
if (Test-Path $log) { Get-Content $log }
if ($p.ExitCode -ne 0) { throw "smoke test failed: $Exe exited with $($p.ExitCode)" }
if (-not (Test-Path (Join-Path $env:PIPELINE_WORKSPACE_DIR "projects"))) { throw "projects folder was not created in the workspace" }
Write-Host "smoke test passed: $Exe"
