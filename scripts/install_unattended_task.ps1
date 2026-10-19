param([Parameter(Mandatory=$true)][ValidatePattern('^\d{4}-\d{2}-\d{2}$')][string]$Date)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Project Python missing' }
$day = [DateTimeOffset]::ParseExact(($Date+'T00:00:00+08:00'),'yyyy-MM-ddTHH:mm:sszzz',[Globalization.CultureInfo]::InvariantCulture)
$taskStart = $day.AddHours(-1).LocalDateTime
$taskAction = New-ScheduledTaskAction -Execute $taskPython -Argument ('-m nba_recorder.unattended --date '+$Date) -WorkingDirectory $taskRoot
$taskTrigger = New-ScheduledTaskTrigger -Once -At $taskStart
$taskLogon = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 60) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$taskPrincipal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$taskName = 'NBA-MM-Paper-'+$Date
Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger @($taskTrigger,$taskLogon) -Settings $taskSettings -Principal $taskPrincipal -Description 'Public NBA data and isolated Paper accounts only; no real trading.' -Force | Out-Null
Get-ScheduledTaskInfo -TaskName $taskName | Format-List NextRunTime,LastTaskResult
