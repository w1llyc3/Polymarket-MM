$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Project Python missing' }
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskAction = New-ScheduledTaskAction -Execute $taskPython -Argument '-m nba_recorder.recording_service' -WorkingDirectory $taskRoot
$taskTriggers = @((New-ScheduledTaskTrigger -Daily -At '00:00'), (New-ScheduledTaskTrigger -AtLogOn -User $taskUser))
$taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'NBA-MM-Continuous-Recording' -Action $taskAction -Trigger $taskTriggers -Settings $taskSettings -Principal $taskPrincipal -Description 'Continuous Polymarket.com NBA public data only; pre30/post30; no real orders or wallet access.' -Force | Out-Null
Start-ScheduledTask -TaskName 'NBA-MM-Continuous-Recording'
Get-ScheduledTask -TaskName 'NBA-MM-Continuous-Recording' | Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName 'NBA-MM-Continuous-Recording' | Select-Object NextRunTime,LastTaskResult
