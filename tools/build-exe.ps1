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

& $python -m PyInstaller --noconfirm --clean --distpath dist --workpath build tools\rigwatch.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

$exe = Get-Item (Join-Path $root "dist\RigWatch.exe")
Write-Host ("Built {0} ({1:n1} MB)" -f $exe.FullName, ($exe.Length / 1MB))
