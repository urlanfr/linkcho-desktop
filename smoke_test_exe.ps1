param(
    [string]$ExecutablePath = ""
)

$ErrorActionPreference = "Stop"
$projectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ExecutablePath) {
    $ExecutablePath = Join-Path $projectDirectory "dist\Linkcho Desktop.exe"
}
$resolvedExecutable = (Resolve-Path -LiteralPath $ExecutablePath).Path
$originalPath = $env:PATH
$originalQtPlatform = $env:QT_QPA_PLATFORM
$process = $null

try {
    # Keep only Windows system paths so accidental build-host DLLs cannot hide
    # missing or incompatible libraries in the packaged application.
    $env:PATH = "C:\Windows\System32;C:\Windows"
    $env:QT_QPA_PLATFORM = "offscreen"
    $process = Start-Process -FilePath $resolvedExecutable -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 10
    $process.Refresh()
    if ($process.HasExited) {
        throw "Packaged application exited during startup with code $($process.ExitCode)"
    }
    Write-Host "EXE startup smoke test passed." -ForegroundColor Green
}
finally {
    if ($null -ne $process) {
        $process.Refresh()
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force
        }
    }
    $env:PATH = $originalPath
    if ($null -eq $originalQtPlatform) {
        Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
    }
    else {
        $env:QT_QPA_PLATFORM = $originalQtPlatform
    }
}

