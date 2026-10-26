# One-time setup: create the virtual environment, install pinned dependencies,
# and put an "Apogee" shortcut on the desktop.
# Usage (from the project folder):  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root ".venv"
$launcher = Join-Path $root "launcher.pyw"

if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Host "Creating virtual environment..."
    py -3 -m venv $venv
}
& (Join-Path $venv "Scripts\python.exe") -m pip install --disable-pip-version-check -q -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell

# The app was called "Internship Manager" before it was named Apogee. Remove that shortcut,
# but only if it is ours (points at this project's launcher).
$legacy = Join-Path $desktop "Internship Manager.lnk"
if (Test-Path $legacy) {
    if ($shell.CreateShortcut($legacy).Arguments -like "*$launcher*") {
        Remove-Item $legacy
        Write-Host "Removed old shortcut: $legacy"
    }
}

$shortcutPath = Join-Path $desktop "Apogee.lnk"
$sc = $shell.CreateShortcut($shortcutPath)
$sc.TargetPath = Join-Path $venv "Scripts\pythonw.exe"
$sc.Arguments = '"' + $launcher + '"'
$sc.WorkingDirectory = $root
$sc.Description = "Start or open Apogee, the internship manager"
$sc.Save()
Write-Host "Shortcut created: $shortcutPath"
