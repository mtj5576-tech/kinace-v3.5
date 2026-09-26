$ErrorActionPreference = "Stop"
$env:PYTHONPATH = Join-Path $PSScriptRoot "src"
& python -m kinace_v3 @args
exit $LASTEXITCODE
