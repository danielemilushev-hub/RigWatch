# Creates a "RigWatch" shortcut on your desktop. Uses the standalone exe when it has been built,
# otherwise the Python version (without a console window).
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root "dist\RigWatch.exe"
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $exe) -and -not (Test-Path $pythonw)) {
    Write-Host "Run RigWatch.bat once first so the app can set itself up." -ForegroundColor Yellow
    exit 1
}
$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut((Join-Path $desktop "RigWatch.lnk"))
if (Test-Path $exe) {
    $link.TargetPath = $exe
    $link.WorkingDirectory = Split-Path -Parent $exe
} else {
    $link.TargetPath = $pythonw
    $link.Arguments = "-m rigwatch"
    $link.WorkingDirectory = $root
}
$link.IconLocation = (Join-Path $root "rigwatch\ui\assets\icon.ico")
$link.Description = "RigWatch hardware monitor"
$link.Save()
Write-Host "Created $($link.FullName)"
