# Create a "LokalProtokoll" shortcut on the desktop and in the Start menu.
# Usage (from the project folder):  powershell -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    Write-Host "Virtual environment not found. Follow the setup in README.md first." -ForegroundColor Red
    exit 1
}

$shell = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "LokalProtokoll.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "LokalProtokoll.lnk")
)
foreach ($path in $targets) {
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $pythonw
    $link.Arguments = "`"$(Join-Path $root 'lp.py')`" app"
    $link.WorkingDirectory = $root
    $link.Description = "Local meeting recorder and summarizer"
    $link.Save()
    Write-Host "Created: $path"
}
