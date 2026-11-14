<#
Creates or updates one daily local Task Scheduler job for Health Hub's Garmin sync.

Run from PowerShell in the Health Hub folder:
  powershell -ExecutionPolicy Bypass -File .\scripts\register_garmin_sync_task.ps1

This script does not contain or print your Garmin key. It expects the local .env
file to already exist in the Health Hub root.
#>

param(
    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')]
    [string]$Time = "23:00",
    [ValidateRange(1, 30)]
    [int]$Days = 2,
    [string]$TaskName = "Health Hub - Garmin Daily Sync"
)

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$syncScript = Join-Path $projectRoot "garmin_sync.py"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Could not find $python. Create the project virtual environment before scheduling the sync."
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot ".env"))) {
    throw "Could not find $projectRoot\.env. Add your Garmin key locally before scheduling a sync."
}

$taskAction = '"{0}" "{1}" --days {2}' -f $python, $syncScript, $Days
& schtasks.exe /Create /TN $TaskName /TR $taskAction /SC DAILY /ST $Time /F | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Task Scheduler could not create the task. Open PowerShell as Administrator and try again."
}

Write-Host "Created '$TaskName' for $Time each day. Test it with:" -ForegroundColor Green
Write-Host "schtasks.exe /Run /TN `"$TaskName`""
