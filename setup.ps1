# LokalProtokoll setup for a clean Windows 10/11 machine.
# Installs what is missing and skips what is already there, so it is safe to run again.
#
# Usage, from the project folder:
#   powershell -ExecutionPolicy Bypass -File setup.ps1
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -CpuOnly     # no GPU build of whisper.cpp
#
# Steps: Python, ffmpeg and Ollama (with winget), the Python environment, whisper.cpp
# (built with Vulkan for AMD/Intel/NVIDIA GPUs, or a ready-made CPU version), the speech
# and speaker models, the summary model, and a desktop shortcut.

param(
    [switch]$CpuOnly,        # use the prebuilt CPU version of whisper.cpp (slower, no build tools needed)
    [switch]$SkipShortcut
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "   OK  $text" -ForegroundColor Green }
function Info($text) { Write-Host "   $text" }

function Refresh-Path {
    # Programs installed with winget are not on PATH in this window until it is refreshed.
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Have($command) { return [bool](Get-Command $command -ErrorAction SilentlyContinue) }

function Winget-Install($id, $name) {
    if (-not (Have "winget")) {
        throw "winget is not available. Install $name by hand (see README.md) and run this script again."
    }
    Info "Installing $name with winget ..."
    winget install --id $id --exact --accept-source-agreements --accept-package-agreements --silent
    if ($LASTEXITCODE -ne 0) { throw "Installing $name failed (winget exit code $LASTEXITCODE)." }
    Refresh-Path
}

# ---------------------------------------------------------------- 1. Python
Step "Python 3.11 or newer"
$python = $null
foreach ($candidate in @("python", "py")) {
    if (Have $candidate) {
        $version = & $candidate -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $version -and [version]$version -ge [version]"3.11") {
            $python = $candidate
            break
        }
    }
}
if (-not $python) {
    Winget-Install "Python.Python.3.12" "Python 3.12"
    $python = "python"
}
Ok "$python $(& $python --version)"

# ---------------------------------------------------------------- 2. ffmpeg
Step "ffmpeg"
if (-not (Have "ffmpeg")) { Winget-Install "Gyan.FFmpeg" "ffmpeg" }
if (-not (Have "ffmpeg")) { throw "ffmpeg is still not found. Open a new PowerShell window and run the script again." }
Ok (ffmpeg -version | Select-Object -First 1)

# ---------------------------------------------------------------- 3. Ollama
Step "Ollama (summaries)"
if (-not (Have "ollama")) { Winget-Install "Ollama.Ollama" "Ollama" }
if (-not (Have "ollama")) { throw "Ollama is still not found. Open a new PowerShell window and run the script again." }
Ok (ollama --version | Select-Object -First 1)

# ---------------------------------------------------------------- 4. Python environment
Step "Python environment (.venv)"
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    & $python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Creating the environment failed." }
}
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Installing the Python packages failed." }
Ok "packages from requirements.txt"

# ---------------------------------------------------------------- 5. whisper.cpp
Step "whisper.cpp (speech to text)"
$whisperCli = Join-Path $root "tools\whisper.cpp\build\bin\Release\whisper-cli.exe"
$cpuCli = Join-Path $root "tools\whisper-cpu\Release\whisper-cli.exe"
if (Test-Path $whisperCli) {
    Ok "already built: $whisperCli"
} elseif ($CpuOnly) {
    if (-not (Test-Path $cpuCli)) {
        Info "Downloading the prebuilt CPU version ..."
        $release = Invoke-RestMethod "https://api.github.com/repos/ggml-org/whisper.cpp/releases?per_page=10"
        $asset = $release | ForEach-Object { $_.assets } | Where-Object { $_.name -eq "whisper-bin-x64.zip" } |
                 Select-Object -First 1
        if (-not $asset) { throw "Could not find whisper-bin-x64.zip in the whisper.cpp releases." }
        $zip = Join-Path $env:TEMP "whisper-bin-x64.zip"
        Invoke-WebRequest $asset.browser_download_url -OutFile $zip
        Expand-Archive $zip -DestinationPath (Join-Path $root "tools\whisper-cpu") -Force
        Remove-Item $zip
    }
    # Point config.toml at the CPU version.
    $config = Get-Content config.toml -Raw
    $config = $config -replace 'whisper_cli = "[^"]*"', 'whisper_cli = "tools/whisper-cpu/Release/whisper-cli.exe"'
    Set-Content config.toml $config -NoNewline -Encoding ascii
    Ok "CPU version: $cpuCli (config.toml updated)"
} else {
    # The Vulkan build needs Git, the Visual Studio C++ build tools (with CMake) and the Vulkan SDK.
    if (-not (Have "git")) { Winget-Install "Git.Git" "Git" }
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    $vs = $null
    if (Test-Path $vswhere) {
        $vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    }
    if (-not $vs) {
        Info "Installing the Visual Studio C++ build tools (this takes a while) ..."
        winget install --id Microsoft.VisualStudio.2022.BuildTools --exact --accept-source-agreements `
            --accept-package-agreements --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
        if ($LASTEXITCODE -ne 0) { throw "Installing the build tools failed." }
    }
    if (-not [Environment]::GetEnvironmentVariable("VULKAN_SDK", "Machine")) {
        Winget-Install "KhronosGroup.VulkanSDK" "the Vulkan SDK"
    }
    & powershell -ExecutionPolicy Bypass -File (Join-Path $root "scripts\build_whispercpp.ps1")
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $whisperCli)) {
        throw "Building whisper.cpp failed. Run the script again with -CpuOnly to use the CPU version instead."
    }
    Ok "built with Vulkan: $whisperCli"
}

# ---------------------------------------------------------------- 6. models
Step "Speech and speaker models (about 1.7 GB)"
& $venvPython setup_models.py
if ($LASTEXITCODE -ne 0) { throw "Downloading the models failed." }
Ok "models\"

# ---------------------------------------------------------------- 7. summary model
Step "Summary model (Ollama)"
$llm = & $venvPython -c "import tomllib; print(tomllib.load(open('config.toml', 'rb'))['summarize']['model'])"
$installed = ollama list | Select-String -SimpleMatch $llm
if ($installed) {
    Ok "$llm is installed"
} else {
    Info "Downloading $llm ..."
    ollama pull $llm
    if ($LASTEXITCODE -ne 0) { throw "Downloading $llm failed. Is the Ollama app running?" }
    Ok $llm
}

# ---------------------------------------------------------------- 8. shortcut
if (-not $SkipShortcut) {
    Step "Shortcut"
    & powershell -ExecutionPolicy Bypass -File (Join-Path $root "scripts\create_shortcut.ps1")
}

Step "Done"
Info "Start LokalProtokoll from the desktop shortcut, or with:  .venv\Scripts\python.exe lp.py app"
Info "Recording devices can be checked with:                 .venv\Scripts\python.exe lp.py devices"
