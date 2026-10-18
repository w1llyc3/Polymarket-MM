$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Project Python missing' }
$taskAction = New-ScheduledTaskAction -Execute $taskPython -Argument '-m nba_recorder.unattended --date 2026-10-19' -WorkingDirectory $taskRoot
# Explicit +08:00 converted to the Windows host timezone, never assume host timezone.
$taskStart = ([DateTimeOffset]::Parse('2026-10-18T23:00:00+08:00')).LocalDateTime
$taskTrigger = New-ScheduledTaskTrigger -Once -At $taskStart
$taskLogon = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 60) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$taskPrincipal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'NBA-MM-Paper-2026-10-19' -Action $taskAction -Trigger @($taskTrigger,$taskLogon) -Settings $taskSettings -Principal $taskPrincipal -Description 'Public NBA data and isolated Paper accounts only; no real trading.' -Force | Out-Null
Get-ScheduledTask -TaskName 'NBA-MM-Paper-2026-10-19' | Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName 'NBA-MM-Paper-2026-10-19' | Select-Object NextRunTime,LastTaskResult
