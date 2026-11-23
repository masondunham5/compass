<#
One-time setup to run Compass around the clock on this computer, reachable from your phone
through Tailscale (and nowhere else).

Before running:
  - Python 3.12 or newer is installed (python.org; tick "Add python.exe to PATH").
  - Tailscale is installed, signed in, and "Run unattended" is on.
  - In the Tailscale admin console (login.tailscale.com > DNS): MagicDNS and HTTPS Certificates are on.
  - This folder is the Compass project (git clone of your repo).

Run in PowerShell **as Administrator** from the Compass folder:
  powershell -ExecutionPolicy Bypass -File .\scripts\setup_server.ps1 -ImportZip "$HOME\Desktop\compass-move.zip" -BackupFolder "$HOME\My Drive\Compass backups"

What it does (safe to run again; it never overwrites an existing database):
  1. Creates .venv and installs Compass's packages.
  2. Imports your data from compass-move.zip (from scripts\export_for_server.py), if given.
  3. Makes the School To Do sync token and prints it.
  4. Registers "Compass - Server" to start at boot, whether or not anyone is signed in, and restart if it stops.
  5. Registers a daily database backup into -BackupFolder (e.g. your Google Drive folder), if given.
  6. Registers the Garmin daily sync if .env (your Garmin key) is here.
  7. Publishes Compass to your tailnet with HTTPS (tailscale serve) and prints the address.
Compass itself only listens on 127.0.0.1; Tailscale is the only way in.
#>

param(
    [string]$ImportZip,
    [string]$BackupFolder,
    [ValidateRange(1024, 65535)][int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

function Write-Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Open PowerShell as Administrator (right-click > Run as administrator) and run this again." }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot "app.py"))) { throw "Run this from the Compass project folder." }

# 1. Python environment -------------------------------------------------------------------
Write-Step "Python environment"
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) { & py -3 -m venv .venv } else {
        $python = Get-Command python -ErrorAction SilentlyContinue
        if (-not $python) { throw "Python isn't installed. Install Python 3.12+ from python.org (tick 'Add python.exe to PATH'), then run this again." }
        & python -m venv .venv
    }
}
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r requirements.txt
Write-Host "Packages installed."

# 2. Your data ------------------------------------------------------------------------------
Write-Step "Data"
$dataDir = Join-Path $projectRoot "data"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$database = Join-Path $dataDir "compass.db"
if ($ImportZip) {
    if (Test-Path -LiteralPath $database) {
        Write-Host "data\compass.db already exists here, so the zip was NOT imported (nothing overwritten)." -ForegroundColor Yellow
    } else {
        Expand-Archive -LiteralPath $ImportZip -DestinationPath $dataDir
        Write-Host "Imported your data from $ImportZip."
    }
} elseif (-not (Test-Path -LiteralPath $database)) {
    Write-Host "No data yet: Compass starts empty (or re-run with -ImportZip)." -ForegroundColor Yellow
}

# 3. School To Do sync token -----------------------------------------------------------------
Write-Step "School To Do sync"
$tokenFile = Join-Path $dataDir "school_sync_token.txt"
if (-not (Test-Path -LiteralPath $tokenFile)) {
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    [Convert]::ToBase64String($bytes).Replace("+", "-").Replace("/", "_").TrimEnd("=") | Set-Content -LiteralPath $tokenFile -Encoding ascii
}
$token = (Get-Content -LiteralPath $tokenFile -Raw).Trim()

# 4. Server task ------------------------------------------------------------------------------
Write-Step "Compass server (starts at boot)"
$pythonw = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
$user = "$env:USERDOMAIN\$env:USERNAME"
$credential = Get-Credential -UserName $user -Message "Your Windows password for $user (so Compass can start before anyone signs in)"
$password = $credential.GetNetworkCredential().Password
$serverAction = New-ScheduledTaskAction -Execute $pythonw -Argument "-m uvicorn app:app --host 127.0.0.1 --port $Port --log-level warning" -WorkingDirectory $projectRoot
$always = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "Compass - Server" -Action $serverAction -Trigger (New-ScheduledTaskTrigger -AtStartup) -Settings $always `
    -User $credential.UserName -Password $password -RunLevel Limited -Description "Runs Compass on 127.0.0.1:$Port." -Force | Out-Null
Stop-ScheduledTask -TaskName "Compass - Server" -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName "Compass - Server"
Start-Sleep -Seconds 6
try {
    Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/" -TimeoutSec 15 | Out-Null
    Write-Host "Compass is running on this computer." -ForegroundColor Green
} catch {
    Write-Host "Compass didn't answer yet. Check Task Scheduler > 'Compass - Server' (Last Run Result)." -ForegroundColor Yellow
}

# 5. Daily off-site backup -------------------------------------------------------------------
if ($BackupFolder) {
    Write-Step "Daily backup to $BackupFolder"
    New-Item -ItemType Directory -Force -Path $BackupFolder | Out-Null
    $backupAction = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$(Join-Path $projectRoot 'scripts\backup_database.py')`" --to `"$BackupFolder`" --keep 30" -WorkingDirectory $projectRoot
    $daily = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask -TaskName "Compass - Daily Backup" -Action $backupAction -Trigger (New-ScheduledTaskTrigger -Daily -At 3am) -Settings $daily `
        -User $credential.UserName -Password $password -RunLevel Limited -Description "Copies the Compass database to $BackupFolder (keeps 30)." -Force | Out-Null
    & $venvPython (Join-Path $projectRoot "scripts\backup_database.py") --to $BackupFolder --keep 30
}
$password = $null

# 6. Garmin ----------------------------------------------------------------------------------
if (Test-Path -LiteralPath (Join-Path $projectRoot ".env")) {
    Write-Step "Garmin daily sync"
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "register_garmin_sync_task.ps1")
} else {
    Write-Host "`nNo .env here, so Garmin sync isn't scheduled. Copy your .env over and run scripts\register_garmin_sync_task.ps1 to add it." -ForegroundColor Yellow
}

# 7. Tailscale (HTTPS, your devices only) ----------------------------------------------------
Write-Step "Tailscale"
$tailscale = @("$env:ProgramFiles\Tailscale\tailscale.exe", "${env:ProgramFiles(x86)}\Tailscale\tailscale.exe") | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
$address = $null
if (-not $tailscale) {
    Write-Host "Tailscale isn't installed. Install it, sign in, then run: tailscale serve --bg $Port" -ForegroundColor Yellow
} else {
    & $tailscale serve --bg $Port
    if ($LASTEXITCODE -ne 0) {
        Write-Host "tailscale serve failed. Turn on MagicDNS and HTTPS Certificates at login.tailscale.com > DNS, then run: tailscale serve --bg $Port" -ForegroundColor Yellow
    }
    $status = & $tailscale status --json | ConvertFrom-Json
    $address = "https://" + $status.Self.DNSName.TrimEnd(".")
}

Write-Step "Done"
if ($address) { Write-Host "Open Compass from any of your devices on Tailscale:  $address" -ForegroundColor Green }
Write-Host "School To Do sync token (keep it private):  $token"
Write-Host "`nOn the PC where you use School To Do, in the Compass folder:"
Write-Host "  ..\.venv\Scripts\python.exe scripts\push_school_todo.py --setup $(if ($address) { $address } else { 'https://<this-computer>.<tailnet>.ts.net' }) $token"
Write-Host "  powershell -ExecutionPolicy Bypass -File .\scripts\register_school_push_task.ps1"
Write-Host "`nThen in Compass on the server: More > Settings > Personal Outlook (connect again) and AI assistant (paste the Groq key again)."
