param(
    [switch]$InstallPyInstaller
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$ReleaseRoot = Join-Path $Root "release"
$BuildRoot = Join-Path $Root "build\readonly-exe"
$EntryPoint = Join-Path $PSScriptRoot "readonly_exe_entry.py"
$ExecutableName = "Kinace-V3.3-ReadOnly-Portal"

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
$env:PYTHONPATH = Join-Path $Root "src"

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --console `
    --name $ExecutableName `
    --paths (Join-Path $Root "src") `
    --hidden-import kinace_v3.portal_v33_assets `
    --hidden-import winreg `
    --add-data "$(Join-Path $Root 'src\kinace_v3\portal_v33_assets');kinace_v3\portal_v33_assets" `
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
