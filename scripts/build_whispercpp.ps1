# Build whisper.cpp with the Vulkan backend (AMD GPU) into tools\whisper.cpp.
# Needs: Git, Visual Studio Build Tools with C++ (includes CMake), Vulkan SDK.
# Usage (from the project folder):  powershell -ExecutionPolicy Bypass -File scripts\build_whispercpp.ps1
# Optional: -Tag v1.9.4 to build a specific release.

param([string]$Tag = "v1.9.4")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $root "tools\whisper.cpp"

# Vulkan SDK: the installer sets VULKAN_SDK, but a shell opened before the
# install does not see it yet, so read it from the registry as well.
if (-not $env:VULKAN_SDK) {
    $env:VULKAN_SDK = [Environment]::GetEnvironmentVariable("VULKAN_SDK", "Machine")
}
if (-not $env:VULKAN_SDK) {
    Write-Host "Vulkan SDK not found. Install it with:" -ForegroundColor Red
    Write-Host "  winget install KhronosGroup.VulkanSDK"
    Write-Host "then open a new PowerShell window and run this script again."
    exit 1
}
Write-Host "Vulkan SDK: $env:VULKAN_SDK"

# CMake: use the one on PATH, otherwise the copy bundled with Visual Studio.
$cmake = (Get-Command cmake -ErrorAction SilentlyContinue).Source
if (-not $cmake) {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
        $candidate = Join-Path $vs "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
        if (Test-Path $candidate) { $cmake = $candidate }
    }
}
if (-not $cmake) {
    Write-Host "CMake not found. Install Visual Studio Build Tools with 'Desktop development with C++'." -ForegroundColor Red
    exit 1
}
Write-Host "CMake: $cmake"

if (-not (Test-Path $src)) {
    git clone --depth 1 --branch $Tag https://github.com/ggml-org/whisper.cpp.git $src
    if ($LASTEXITCODE -ne 0) { exit 1 }
} else {
    Write-Host "Using existing source in $src"
}

& $cmake -S $src -B "$src\build" -DGGML_VULKAN=1 -DWHISPER_BUILD_TESTS=OFF
if ($LASTEXITCODE -ne 0) { exit 1 }
& $cmake --build "$src\build" --config Release -j
if ($LASTEXITCODE -ne 0) { exit 1 }

$exe = Join-Path $src "build\bin\Release\whisper-cli.exe"
if (Test-Path $exe) {
    Write-Host "Built: $exe" -ForegroundColor Green
    & $exe --help | Select-Object -First 3
} else {
    Write-Host "Build finished but whisper-cli.exe was not found." -ForegroundColor Red
    exit 1
}
