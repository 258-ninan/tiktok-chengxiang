$ErrorActionPreference = 'Stop'
$taskName = 'Chengxian-TikTok-Local'
$pwshPath = (Get-Command pwsh -ErrorAction Stop).Source
$taskScript = Join-Path $PSScriptRoot 'local-scheduled.ps1'
$taskArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -File "' + $taskScript + '"'
$taskAction = New-ScheduledTaskAction -Execute $pwshPath -Argument $taskArguments -WorkingDirectory $PSScriptRoot
$localOptions = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'local_config.json') -Raw | ConvertFrom-Json
$taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes ($localOptions.scheduled_run_minutes + 5)) -StartWhenAvailable -RunOnlyIfNetworkAvailable
$taskTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $localOptions.scheduled_retry_minutes)
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
$taskDefinition = @{
    TaskName = $taskName
    Action = $taskAction
    Settings = $taskSettings
    Trigger = $taskTrigger
    Principal = $taskPrincipal
    Description = 'Local TikTok raw oud research, using the signed-in Windows session'
    Force = $true
}
Register-ScheduledTask @taskDefinition | Out-Null
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName $taskName | Select-Object NextRunTime
