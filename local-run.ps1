$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
Push-Location -LiteralPath $PSScriptRoot
try {
    & "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\local_collect.py" run @args
    $collectorExit = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $collectorExit
