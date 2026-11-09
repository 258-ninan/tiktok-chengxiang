$ErrorActionPreference = 'Stop'
$logFolder = Join-Path $PSScriptRoot 'data\logs'
New-Item -ItemType Directory -Path $logFolder -Force | Out-Null
$logPath = Join-Path $logFolder ('chrome-scheduled-' + (Get-Date -Format 'yyyyMMdd') + '.log')
$localOptions = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'local_config.json') -Raw | ConvertFrom-Json
& "$PSScriptRoot\chrome-run.ps1" --scheduled --duration-minutes $localOptions.scheduled_run_minutes *>> $logPath
exit $LASTEXITCODE
