$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
& "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\chrome_bridge.py" @args
exit $LASTEXITCODE
