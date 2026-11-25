# Builds dist\RigWatch.exe: a single file that runs on Windows PCs without Python installed.
# Usage:  powershell -ExecutionPolicy Bypass -File tools\build-exe.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv-build\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Creating the build environment (.venv-build)..."
    python -m venv .venv-build
}
& $python -m pip install --disable-pip-version-check -q -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "Could not install the build requirements." }

# The sensor helper's engine (CPU temperature) is not kept in git; fetch it so the exe includes it.
if (-not (Test-Path (Join-Path $root "rigwatch\vendor\lhm\LibreHardwareMonitorLib.dll"))) {
    Write-Host "Fetching the sensor engine (CPU temperature)..."
    & $python tools\fetch-sensor-engine.py
    if ($LASTEXITCODE -ne 0) { throw "Could not fetch the sensor engine." }
}

& $python -m PyInstaller --noconfirm --clean --distpath dist --workpath build tools\rigwatch.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

$exe = Get-Item (Join-Path $root "dist\RigWatch.exe")
Write-Host ("Built {0} ({1:n1} MB)" -f $exe.FullName, ($exe.Length / 1MB))
