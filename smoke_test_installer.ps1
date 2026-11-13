param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath
)

$ErrorActionPreference = "Stop"
$resolvedInstaller = (Resolve-Path -LiteralPath $InstallerPath).Path
$uninstallKey = (
    "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\" +
    "{A0B26B30-328D-4B9D-B47F-3B45C4E3E09E}_is1"
)
if (Test-Path -LiteralPath $uninstallKey) {
    throw "Linkcho Desktop is already installed for this user; installer smoke test aborted to protect it."
}

$testRoot = Join-Path $env:TEMP ("linkcho-installer-smoke-" + $PID)
$installLog = Join-Path $env:TEMP ("linkcho-installer-smoke-" + $PID + ".log")
if (Test-Path -LiteralPath $testRoot) {
    throw "Smoke-test directory already exists: $testRoot"
}

$originalPath = $env:PATH
$originalQtPlatform = $env:QT_QPA_PLATFORM
try {
    $arguments = (
        '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER ' +
        '/DIR="' + $testRoot + '" /LOG="' + $installLog + '"'
    )
    $setup = Start-Process `
        -FilePath $resolvedInstaller `
        -ArgumentList $arguments `
        -PassThru `
        -Wait `
        -WindowStyle Hidden
    if ($setup.ExitCode -ne 0) {
        throw "Installer exited with code $($setup.ExitCode). Log: $installLog"
    }

    $installedExecutable = Join-Path $testRoot "Linkcho Desktop.exe"
    if (-not (Test-Path -LiteralPath $installedExecutable)) {
        throw "Installer did not create the application executable."
    }

    $env:PATH = "C:\Windows\System32;C:\Windows"
    $env:QT_QPA_PLATFORM = "offscreen"
    $application = Start-Process `
        -FilePath $installedExecutable `
        -PassThru `
        -WindowStyle Hidden
    Start-Sleep -Seconds 10
    $application.Refresh()
    if ($application.HasExited) {
        throw "Installed application exited during startup with code $($application.ExitCode)"
    }

    # A PyInstaller one-file app has a bootloader and an application process.
    # Stop every process launched from the installed executable before uninstall.
    $matchingProcesses = @(
        Get-Process | Where-Object {
            try { $_.Path -eq $installedExecutable } catch { $false }
        }
    )
    foreach ($process in $matchingProcesses) {
        Stop-Process -Id $process.Id -Force
    }
    Start-Sleep -Seconds 2

    $uninstaller = Join-Path $testRoot "unins000.exe"
    if (-not (Test-Path -LiteralPath $uninstaller)) {
        throw "Installer did not create its uninstaller."
    }
    $uninstall = Start-Process `
        -FilePath $uninstaller `
        -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART" `
        -PassThru `
        -Wait `
        -WindowStyle Hidden
    if ($uninstall.ExitCode -ne 0) {
        throw "Uninstaller exited with code $($uninstall.ExitCode)"
    }
    Start-Sleep -Seconds 2

    if (Test-Path -LiteralPath $testRoot) {
        $remaining = @(Get-ChildItem -LiteralPath $testRoot -Force)
        if ($remaining.Count -gt 0) {
            throw "Uninstaller left $($remaining.Count) item(s) in $testRoot"
        }
        Remove-Item -LiteralPath $testRoot -Force
    }
    if (Test-Path -LiteralPath $uninstallKey) {
        throw "Uninstaller left its Windows uninstall registration behind."
    }
    Write-Host "Installer install/start/uninstall smoke test passed." -ForegroundColor Green
}
finally {
    $env:PATH = $originalPath
    if ($null -eq $originalQtPlatform) {
        Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
    }
    else {
        $env:QT_QPA_PLATFORM = $originalQtPlatform
    }
}

