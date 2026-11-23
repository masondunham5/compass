<#
Run on the PC where you use School To Do (not the server), after
  ..\.venv\Scripts\python.exe scripts\push_school_todo.py --setup URL TOKEN
worked once. Creates a task that sends School To Do changes to Compass every 10 minutes
while you're signed in (it only sends when something changed).

  powershell -ExecutionPolicy Bypass -File .\scripts\register_school_push_task.ps1
#>

param([string]$TaskName = "Compass - Push School To Do", [ValidateRange(5, 120)][int]$Minutes = 10)

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = @((Join-Path $projectRoot ".venv\Scripts\pythonw.exe"), (Join-Path (Split-Path -Parent $projectRoot) ".venv\Scripts\pythonw.exe")) |
    Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $python) { throw "Couldn't find Python in .venv or ..\.venv." }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot "data\school_push.json"))) {
    throw "Run scripts\push_school_todo.py --setup URL TOKEN first."
}

$action = New-ScheduledTaskAction -Execute $python -Argument "`"$(Join-Path $projectRoot 'scripts\push_school_todo.py')`"" -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Sends School To Do changes to Compass." -Force | Out-Null
Write-Host "Created '$TaskName' (every $Minutes minutes while you're signed in)." -ForegroundColor Green
