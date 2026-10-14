# Local part of the daily routine (invoked by the "JobApplyAgent" scheduled task at logon and 08:00).
# The pipeline itself runs on Modal at 07:00 IST; this only pulls the new resume PDFs from Drive to
# the Desktop (scripts\sync_desktop.py). Logs to output\logs\<date>-sync.log.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$logDir = Join-Path $root "output\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ("{0:yyyy-MM-dd}-sync.log" -f (Get-Date))

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
& "$root\.venv\Scripts\python.exe" "$root\scripts\sync_desktop.py" *>> $log
exit $LASTEXITCODE
