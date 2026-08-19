[CmdletBinding()]
param(
    [switch]$DirOnly
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$appRoot = Join-Path $projectRoot "app"
$pocRoot = Join-Path $projectRoot "poc"
$stagingRoot = Join-Path $projectRoot "release\staging"
$runtimeRoot = Join-Path $stagingRoot "runtime"
$runtimePocRoot = Join-Path $runtimeRoot "poc"

function Require-Path {
    param([string]$Path, [string]$Message)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw $Message
    }
}

function Copy-RuntimeFile {
    param([string]$Name)
    $source = Join-Path $pocRoot $Name
    Require-Path $source "Missing runtime file: $source"
    Copy-Item -LiteralPath $source -Destination (Join-Path $runtimePocRoot $Name) -Force
}

Set-Location -LiteralPath $projectRoot

Require-Path (Join-Path $appRoot "package.json") "app\package.json is missing."
Require-Path (Join-Path $appRoot "node_modules\electron-builder\package.json") "electron-builder is missing. Run: cd app; npm install"
Require-Path (Join-Path $pocRoot ".venv\Scripts\python.exe") "poc\.venv is missing. Install the Python runtime first."
Require-Path (Join-Path $pocRoot "config.example.py") "poc\config.example.py is missing."

$package = Get-Content -Raw -LiteralPath (Join-Path $appRoot "package.json") | ConvertFrom-Json
$version = $package.version
if (-not $version) {
    throw "app/package.json has no version field."
}
$versionTag = "V$version"
$outputRoot = Join-Path $projectRoot "release\$versionTag"

Write-Host ""
Write-Host "========================================"
Write-Host "  Meeting Copilot - $versionTag Windows Pack"
Write-Host "========================================"
Write-Host ""

$requiredModels = @(
    "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
    "silero_vad_v5.onnx"
)
foreach ($model in $requiredModels) {
    Require-Path (Join-Path $pocRoot "models\$model") "Missing local model: poc\models\$model"
}

if (Test-Path -LiteralPath $stagingRoot) {
    $resolvedStaging = (Resolve-Path -LiteralPath $stagingRoot).Path
    $resolvedRelease = (Resolve-Path -LiteralPath (Join-Path $projectRoot "release")).Path
    if (-not $resolvedStaging.StartsWith($resolvedRelease, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean staging path outside the workspace: $resolvedStaging"
    }
    Remove-Item -LiteralPath $stagingRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $runtimePocRoot -Force | Out-Null

# Copy only Python files needed at runtime; never include local config.py or API keys.
$runtimePythonFiles = @(
    "asr_aliyun.py", "asr_mimo.py", "asr_tencent.py", "asr_volcano.py",
    "asr_xfyun.py", "asr_xfyun_llm.py", "asr_hotwords.py", "audio_clock.py",
    "audio_recorder.py", "base.py", "clean_transcript.py", "desktop_bridge.py",
    "diarize_offline.py", "document_extract.py", "enroll_voice.py", "generate_minutes.py",
    "generate_review.py", "knowledge_base.py", "mic_stream.py", "model_catalog.py",
    "online_audio_stream.py", "providers.py", "service_check.py", "speaker_me.py",
    "suggest.py", "turn_split.py", "warmup_meeting.py"
)
foreach ($file in $runtimePythonFiles) { Copy-RuntimeFile $file }
Copy-Item -LiteralPath (Join-Path $pocRoot "config.example.py") -Destination (Join-Path $runtimePocRoot "config.py") -Force
Copy-Item -LiteralPath (Join-Path $pocRoot "requirements.txt") -Destination (Join-Path $runtimePocRoot "requirements.txt") -Force

New-Item -ItemType Directory -Path (Join-Path $runtimePocRoot "models") -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $pocRoot "models\README.md") -Destination (Join-Path $runtimePocRoot "models\README.md") -Force
foreach ($model in $requiredModels) {
    Copy-Item -LiteralPath (Join-Path $pocRoot "models\$model") -Destination (Join-Path $runtimePocRoot "models\$model") -Force
}

Write-Host "[1/3] Building renderer..." -ForegroundColor Cyan
Push-Location -LiteralPath $appRoot
try {
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw "Renderer build failed." }
}
finally {
    Pop-Location
}

Write-Host "[2/3] Checking staged Python runtime..." -ForegroundColor Cyan
$stagedPython = Join-Path $runtimePocRoot ".venv\Scripts\python.exe"
Copy-Item -LiteralPath (Join-Path $pocRoot ".venv") -Destination $runtimePocRoot -Recurse -Force
Require-Path $stagedPython "Python runtime copy failed."

# Remove development-only payload from the copied venv. Native DLLs and python.exe
# remain; pip/setuptools/CLI wrappers, headers and bytecode caches are not needed.
$stagedVenv = Join-Path $runtimePocRoot ".venv"
$stagedScripts = Join-Path $stagedVenv "Scripts"
Get-ChildItem -LiteralPath $stagedScripts -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notin @("python.exe", "pythonw.exe") -and $_.Extension -ne ".dll" } |
    Remove-Item -Force
Remove-Item -LiteralPath (Join-Path $stagedVenv "Include") -Recurse -Force -ErrorAction SilentlyContinue
$stagedSitePackages = Join-Path $stagedVenv "Lib\site-packages"
Remove-Item -LiteralPath (Join-Path $stagedSitePackages "pip") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $stagedSitePackages "setuptools") -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -LiteralPath $stagedSitePackages -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "pip-*.dist-info" -or $_.Name -like "setuptools-*.dist-info" } |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $stagedVenv -Directory -Filter "__pycache__" -Recurse -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $stagedVenv -File -Filter "*.pyc" -Recurse -ErrorAction SilentlyContinue |
    Remove-Item -Force

Push-Location -LiteralPath $runtimePocRoot
try {
    $env:PYTHONIOENCODING = "utf-8"
    & $stagedPython -X utf8 -c "import numpy, sounddevice, sherpa_onnx, pdfplumber; print('staged Python runtime imports: ok')"
    if ($LASTEXITCODE -ne 0) { throw "Staged Python runtime import check failed." }
    & $stagedPython -X utf8 -c "import config, speaker_me; assert config.SAMPLE_RATE == 16000; print('staged Python config/models: ok')"
    if ($LASTEXITCODE -ne 0) { throw "Staged Python config/model path check failed." }
    $serviceStatusJson = & $stagedPython -X utf8 "service_check.py" "--status"
    if ($LASTEXITCODE -ne 0) { throw "Staged service_check.py failed." }
    $serviceStatus = ($serviceStatusJson | Select-Object -Last 1 | ConvertFrom-Json)
    if (-not $serviceStatus.providers -or -not $serviceStatus.retrieval) {
        throw "Staged service_check.py returned incomplete service status."
    }
}
finally {
    Pop-Location
}

Write-Host "[3/3] Building Windows package..." -ForegroundColor Cyan
Push-Location -LiteralPath $appRoot
try {
    $builderArgs = @("electron-builder", "--config", "electron-builder.yml", "--win", "nsis", "--x64")
    if ($DirOnly) { $builderArgs = @("electron-builder", "--config", "electron-builder.yml", "--dir", "--x64") }
    & npx.cmd --yes @builderArgs
    if ($LASTEXITCODE -ne 0) { throw "electron-builder build failed." }
}
finally {
    Pop-Location
}

Write-Host ""
Write-Host "Package complete." -ForegroundColor Green
Get-ChildItem -LiteralPath $outputRoot -File | Select-Object Name,Length,LastWriteTime | Format-Table
