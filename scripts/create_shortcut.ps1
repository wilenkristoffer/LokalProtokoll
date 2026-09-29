# Create a "LokalProtokoll" shortcut on the desktop and in the Start menu.
# Usage (from the project folder):  powershell -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    Write-Host "Virtual environment not found. Follow the setup in README.md first." -ForegroundColor Red
    exit 1
}

# The same icon as the window and the tray (written to the project folder).
$python = Join-Path $root ".venv\Scripts\python.exe"
$icon = Join-Path $root "LokalProtokoll.ico"
Push-Location $root
& $python -c "import sys; from lokalprotokoll.app import write_icon; write_icon(sys.argv[1])" $icon
Pop-Location
if (-not (Test-Path $icon)) {
    Write-Host "Could not create the icon; the shortcut gets Python's icon." -ForegroundColor Yellow
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
    if (Test-Path $icon) {
        $link.IconLocation = "$icon,0"
    }
    $link.Save()
    Write-Host "Created: $path"
}
