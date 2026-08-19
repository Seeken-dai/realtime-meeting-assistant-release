[CmdletBinding()]
param(
    [switch]$Check
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$appRoot = Join-Path $projectRoot "app"
$pythonPath = Join-Path $projectRoot "poc\.venv\Scripts\python.exe"
$configPath = Join-Path $projectRoot "poc\config.py"
$electronPackage = Join-Path $appRoot "node_modules\electron\package.json"
$electronRuntime = Join-Path $appRoot "node_modules\electron\dist\electron.exe"
$electronInstaller = Join-Path $appRoot "node_modules\electron\install.js"

Set-Location -LiteralPath $projectRoot

Write-Host ""
Write-Host "========================================"
Write-Host "  Meeting Copilot - M3 Desktop"
Write-Host "========================================"
Write-Host ""

function Stop-WithMessage {
    param([string]$Message)
    Write-Host "[ERROR] $Message" -ForegroundColor Red
    if (-not $Check) {
        Write-Host ""
        Read-Host "Press Enter to close"
    }
    exit 1
}

if (-not (Test-Path -LiteralPath (Join-Path $appRoot "package.json"))) {
    Stop-WithMessage "app\package.json was not found. Keep this launcher in the project root."
}

$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
if (-not $nodeCommand) {
    Stop-WithMessage "Node.js was not found. Install Node.js 20 or newer."
}

$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npmCommand) {
    Stop-WithMessage "npm was not found."
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    Stop-WithMessage "poc\.venv\Scripts\python.exe was not found."
}

if (-not (Test-Path -LiteralPath $configPath)) {
    Stop-WithMessage "poc\config.py was not found. Copy config.example.py and add local credentials."
}

Write-Host "[OK] Node.js: $(& $nodeCommand.Source --version)" -ForegroundColor Green
Write-Host "[OK] Python: $(& $pythonPath --version 2>&1)" -ForegroundColor Green
Write-Host "[OK] Local config: poc\config.py" -ForegroundColor Green

if ($Check) {
    if (Test-Path -LiteralPath $electronPackage) {
        $electronVersion = (Get-Content -Raw -LiteralPath $electronPackage | ConvertFrom-Json).version
        Write-Host "[OK] Frontend dependencies installed. Electron package $electronVersion" -ForegroundColor Green
        if (Test-Path -LiteralPath $electronRuntime) {
            Write-Host "[OK] Electron runtime installed." -ForegroundColor Green
        }
        else {
            Write-Host "[PENDING] Electron runtime will be downloaded on first launch." -ForegroundColor Yellow
        }
    }
    else {
        Write-Host "[PENDING] Frontend dependencies will be installed on first launch." -ForegroundColor Yellow
    }
    Write-Host ""
    Write-Host "Environment check completed."
    exit 0
}

if (-not (Test-Path -LiteralPath $electronPackage)) {
    Write-Host ""
    Write-Host "[SETUP] Installing frontend dependencies..." -ForegroundColor Yellow
    if (-not $env:ELECTRON_MIRROR) {
        $env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
    }
    Push-Location -LiteralPath $appRoot
    try {
        & $npmCommand.Source install
        if ($LASTEXITCODE -ne 0) {
            Stop-WithMessage "Dependency installation failed. Check the network and retry."
        }
    }
    finally {
        Pop-Location
    }
}

if (-not (Test-Path -LiteralPath $electronRuntime)) {
    Write-Host ""
    Write-Host "[SETUP] Downloading the Electron runtime..." -ForegroundColor Yellow
    if (-not $env:ELECTRON_MIRROR) {
        $env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
    }
    & $nodeCommand.Source $electronInstaller
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $electronRuntime)) {
        Stop-WithMessage "Electron runtime installation failed. Check the network and retry."
    }
    Write-Host "[OK] Electron runtime installed." -ForegroundColor Green
}

Write-Host ""
Write-Host "[START] Opening the M3 desktop app..." -ForegroundColor Cyan
Write-Host "This window will close after the app exits."
Write-Host ""

Push-Location -LiteralPath $appRoot
try {
    & $npmCommand.Source run dev
    $startExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

if ($startExitCode -ne 0) {
    Stop-WithMessage "The app exited unexpectedly with code $startExitCode."
}

exit 0
