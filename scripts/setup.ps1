# One-time setup: create the virtual environment, install pinned dependencies,
# and put an "Internship Manager" shortcut on the desktop.
# Usage (from the project folder):  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root ".venv"

if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Host "Creating virtual environment..."
    py -3 -m venv $venv
}
& (Join-Path $venv "Scripts\python.exe") -m pip install --disable-pip-version-check -q -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "Internship Manager.lnk"
$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($shortcutPath)
$sc.TargetPath = Join-Path $venv "Scripts\pythonw.exe"
$sc.Arguments = '"' + (Join-Path $root "launcher.pyw") + '"'
$sc.WorkingDirectory = $root
$sc.Description = "Start or open the Internship Manager"
$sc.Save()
Write-Host "Shortcut created: $shortcutPath"
