param(
    [switch]$InstallPyInstaller
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$ReleaseRoot = Join-Path $Root "release"
$BuildRoot = Join-Path $Root "build\exe"
$EntryPoint = Join-Path $PSScriptRoot "exe_entry.py"
$ExecutableName = "Kinace-V3.3-Daily"

if ($InstallPyInstaller) {
    python -m pip install --upgrade pyinstaller
}

python -m PyInstaller --version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed. Run with -InstallPyInstaller or install pyinstaller first."
}

if (Test-Path $BuildRoot) {
    Remove-Item -LiteralPath $BuildRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --console `
    --name $ExecutableName `
    --paths (Join-Path $Root "src") `
    --collect-all kinace_v3 `
    --hidden-import kinace_v3.portal_assets `
    --hidden-import winreg `
    --add-data "$(Join-Path $Root 'src\kinace_v3\portal_assets');kinace_v3\portal_assets" `
    --distpath $BuildRoot `
    --workpath (Join-Path $BuildRoot "work") `
    --specpath (Join-Path $BuildRoot "spec") `
    $EntryPoint

$SourceExe = Join-Path $BuildRoot "$ExecutableName.exe"
$TargetExe = Join-Path $ReleaseRoot "$ExecutableName.exe"
Copy-Item -LiteralPath $SourceExe -Destination $TargetExe -Force
$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $TargetExe).Hash.ToLowerInvariant()
$HashPath = Join-Path $ReleaseRoot "$ExecutableName.sha256"
"$Hash  $ExecutableName.exe" | Set-Content -LiteralPath $HashPath -Encoding ascii

[pscustomobject]@{
    Executable = $TargetExe
    Sha256 = $Hash
    Sha256File = $HashPath
} | Format-List
