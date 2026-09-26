param(
    [string]$Version = "3.3.0",
    [string]$PackageName = "kinace-v3.3core"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$ReleaseRoot = Join-Path $Root "release"
$PackageRoot = Join-Path $ReleaseRoot $PackageName
$ArchivePath = Join-Path $ReleaseRoot "$PackageName.zip"
$StagingRoot = Join-Path $ReleaseRoot ".staging"
$StagingPackage = Join-Path $StagingRoot $PackageName

if (Test-Path $StagingRoot) {
    Remove-Item -LiteralPath $StagingRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $StagingPackage | Out-Null

$Files = @(
    ".gitignore",
    "LICENSE",
    "README.md",
    "PROJECT_DESCRIPTION.md",
    "VERSION",
    "pyproject.toml",
    "run.ps1"
)
foreach ($File in $Files) {
    Copy-Item -LiteralPath (Join-Path $Root $File) -Destination $StagingPackage
}

$Directories = @("src", "tests", "docs", "scripts", "config")
foreach ($Directory in $Directories) {
    Copy-Item -LiteralPath (Join-Path $Root $Directory) `
        -Destination (Join-Path $StagingPackage $Directory) `
        -Recurse
}

# Scheduler runtime configuration is required in a clean installation.
$RuntimeReports = @(
    "reports\baseline_v32_candidate.json",
    "reports\baseline_v32_backtest_config.json"
)
$StagingReports = Join-Path $StagingPackage "reports"
New-Item -ItemType Directory -Force -Path $StagingReports | Out-Null
foreach ($Report in $RuntimeReports) {
    Copy-Item -LiteralPath (Join-Path $Root $Report) -Destination $StagingReports
}

Get-ChildItem -LiteralPath $StagingPackage -Recurse -Force -Directory |
    Where-Object { $_.Name -in @("__pycache__", ".pytest_cache") } |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $StagingPackage -Recurse -Force -File |
    Where-Object { $_.Extension -in @(".pyc", ".pyo", ".sqlite3", ".log") } |
    Remove-Item -Force

if (Test-Path $PackageRoot) {
    Remove-Item -LiteralPath $PackageRoot -Recurse -Force
}
Move-Item -LiteralPath $StagingPackage -Destination $PackageRoot
Remove-Item -LiteralPath $StagingRoot -Recurse -Force

if (Test-Path $ArchivePath) {
    Remove-Item -LiteralPath $ArchivePath -Force
}
Compress-Archive -LiteralPath $PackageRoot -DestinationPath $ArchivePath -CompressionLevel Optimal

$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ArchivePath).Hash.ToLowerInvariant()
$SumPath = Join-Path $ReleaseRoot "SHA256SUMS"
$SumLines = @("$Hash  $PackageName.zip")
$DailyExe = Join-Path $ReleaseRoot "Kinace-V3.3-Daily.exe"
if (Test-Path $DailyExe) {
    $ExeHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $DailyExe).Hash.ToLowerInvariant()
    $SumLines += "$ExeHash  Kinace-V3.3-Daily.exe"
}
$ReadOnlyExe = Join-Path $ReleaseRoot "Kinace-V3.3-ReadOnly-Portal.exe"
if (Test-Path $ReadOnlyExe) {
    $ReadOnlyHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ReadOnlyExe).Hash.ToLowerInvariant()
    $SumLines += "$ReadOnlyHash  Kinace-V3.3-ReadOnly-Portal.exe"
}
$SumLines | Set-Content -LiteralPath $SumPath -Encoding ascii

[pscustomobject]@{
    Version = $Version
    PackageDirectory = $PackageRoot
    Archive = $ArchivePath
    Sha256 = $Hash
    Sha256File = $SumPath
} | Format-List
