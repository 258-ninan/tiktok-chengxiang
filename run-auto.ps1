# Run the collector with the current Windows user's encrypted Apify token.
$ErrorActionPreference = 'Stop'
$collectorArgs = $args
$tokenFile = Join-Path $PSScriptRoot '.apify-token.dpapi'
if (-not (Test-Path -LiteralPath $tokenFile -PathType Leaf)) {
    throw 'Encrypted Apify token is missing. See README.md.'
}
$previousToken = $env:APIFY_TOKEN
$secureToken = (Get-Content -LiteralPath $tokenFile -Raw).Trim() | ConvertTo-SecureString
Push-Location -LiteralPath $PSScriptRoot
try {
    $env:APIFY_TOKEN = ([pscredential]::new('apify', $secureToken)).GetNetworkCredential().Password
    if ($collectorArgs.Count -eq 1 -and $collectorArgs[0] -eq '--check-token') {
        python collect.py check-apify
    } else {
        python collect.py auto @collectorArgs
    }
    $collectorExit = $LASTEXITCODE
} finally {
    $env:APIFY_TOKEN = $previousToken
    $secureToken.Dispose()
    Pop-Location
}
exit $collectorExit
