param(
    [switch]$SkipInstall,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$projectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectDirectory
$pythonExecutable = Join-Path $projectDirectory ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    python -m venv .venv
}

if (-not $SkipInstall) {
    & $pythonExecutable -m pip install --upgrade pip
    & $pythonExecutable -m pip install -r requirements-dev.txt
}

if (-not $SkipTests) {
    & $pythonExecutable -m py_compile main.py database.py ai_parser.py tts_service.py
    & $pythonExecutable smoke_test_phase3.py
    & $pythonExecutable smoke_test_phase4.py
}

& $pythonExecutable -m PyInstaller --noconfirm --clean Linkcho.spec
& (Join-Path $projectDirectory "smoke_test_exe.ps1")
Write-Host "Build complete: dist\Linkcho Desktop.exe" -ForegroundColor Green

