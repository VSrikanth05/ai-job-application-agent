# Registers (or updates) the "JobApplyAgent" Windows scheduled task: pulls the resumes the cloud
# (Modal, 07:00 IST) made from Drive to the Desktop — at every logon and daily at 08:00 if logged in.
# Runs as the current user so it can write to the Desktop.
# Remove with:  Unregister-ScheduledTask -TaskName JobApplyAgent
param([string]$At = "08:00")

$script = Join-Path $PSScriptRoot "daily_run.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$daily = New-ScheduledTaskTrigger -Daily -At $At
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logon.Delay = "PT2M"  # give the network a moment after logon
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -RunOnlyIfNetworkAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "JobApplyAgent" -Action $action -Trigger @($daily, $logon) -Settings $settings `
    -Principal $principal -Description "Pull today's tailored resumes from Drive to the Desktop (pipeline runs on Modal)" -Force |
    Select-Object TaskName, State
Write-Host "Sync scheduled at logon and daily at $At. Next run:" (Get-ScheduledTaskInfo -TaskName JobApplyAgent).NextRunTime
