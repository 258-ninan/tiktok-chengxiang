$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
& "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\local_collect.py" login
exit $LASTEXITCODE
