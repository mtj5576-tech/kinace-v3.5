param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("preopen", "postclose", "weekly", "extension", "status")]
    [string]$Mode
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:PYTHONPATH = Join-Path $Root "src"
if (-not $env:TUSHARE_TOKEN) {
    $env:TUSHARE_TOKEN = [Environment]::GetEnvironmentVariable("TUSHARE_TOKEN", "User")
}
if (-not $env:KINACE_ALERT_WEBHOOK_URL) {
    $env:KINACE_ALERT_WEBHOOK_URL = [Environment]::GetEnvironmentVariable("KINACE_ALERT_WEBHOOK_URL", "User")
}

& python -m kinace_v3.schedule_runner --mode $Mode
exit $LASTEXITCODE
