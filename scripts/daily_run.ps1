# Local part of the daily routine (invoked by the "JobApplyAgent" scheduled task at logon and 08:00).
# The pipeline itself runs on Modal at 07:00 IST; this only pulls the new resume PDFs from Drive to
# the Desktop (scripts\sync_desktop.py). Logs to output\logs\<date>-sync.log.
#
# Python's output is captured with Start-Process into temp files, NOT with `*>>`: Windows PowerShell 5.1
# turns every stderr line of a redirected native command into an error record, and under
# $ErrorActionPreference = "Stop" the first log line (Python logging writes to stderr) kills the script.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$logDir = Join-Path $root "output\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ("{0:yyyy-MM-dd}-sync.log" -f (Get-Date))

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
$out = [IO.Path]::GetTempFileName()
$err = [IO.Path]::GetTempFileName()
try {
    $p = Start-Process -FilePath "$root\.venv\Scripts\python.exe" -ArgumentList "`"$root\scripts\sync_desktop.py`"" `
        -WorkingDirectory $root -NoNewWindow -Wait -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
    $code = $p.ExitCode
    Add-Content -Path $log -Encoding UTF8 -Value ("===== sync run {0:yyyy-MM-dd HH:mm:ss} (exit {1})" -f (Get-Date), $code)
    Get-Content -Path $out, $err -Encoding UTF8 | Add-Content -Path $log -Encoding UTF8
} catch {
    Add-Content -Path $log -Encoding UTF8 -Value ("===== sync run {0:yyyy-MM-dd HH:mm:ss} FAILED TO START: {1}" -f (Get-Date), $_)
    $code = 1
} finally {
    Remove-Item -Path $out, $err -ErrorAction SilentlyContinue
}
exit $code
