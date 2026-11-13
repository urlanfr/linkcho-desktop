param(
    [switch]$SkipAppBuild
)

$ErrorActionPreference = "Stop"
$projectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectDirectory

if (-not $SkipAppBuild) {
    & (Join-Path $projectDirectory "build_windows.ps1") -SkipInstall
}

$compilerCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
)
$compiler = $compilerCandidates |
    Where-Object { Test-Path -LiteralPath $_ } |
    Select-Object -First 1
if (-not $compiler) {
    throw "Inno Setup 6 was not found. Install JRSoftware.InnoSetup with winget."
}

& $compiler (Join-Path $projectDirectory "installer.iss")
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup compilation failed with exit code $LASTEXITCODE"
}

$installer = Join-Path $projectDirectory (
    "dist\Linkcho-Desktop-Setup-v1.0.2-windows-x64.exe"
)
if (-not (Test-Path -LiteralPath $installer)) {
    throw "Installer output was not created: $installer"
}
Write-Host "Installer complete: $installer" -ForegroundColor Green
& (Join-Path $projectDirectory "smoke_test_installer.ps1") -InstallerPath $installer
