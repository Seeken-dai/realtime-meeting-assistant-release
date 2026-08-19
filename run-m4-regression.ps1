[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
}
catch {
    # Older hosts may not allow changing the console encoding.
}

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pocRoot = Join-Path $projectRoot "poc"
$appRoot = Join-Path $projectRoot "app"
$pythonPath = Join-Path $pocRoot ".venv\Scripts\python.exe"
$results = [System.Collections.Generic.List[object]]::new()

function Stop-Preflight {
    param([string]$Message)

    Write-Host "[PRECHECK FAILED] $Message" -ForegroundColor Red
    exit 1
}

function Invoke-RegressionStep {
    param(
        [string]$Name,
        [string]$WorkingDirectory,
        [string]$FilePath,
        [string[]]$Arguments
    )

    Write-Host ""
    Write-Host ">>> $Name" -ForegroundColor Cyan
    $startedAt = Get-Date
    $exitCode = 1

    Push-Location -LiteralPath $WorkingDirectory
    try {
        & $FilePath @Arguments
        $exitCode = $LASTEXITCODE
        if ($null -eq $exitCode) {
            $exitCode = 0
        }
    }
    catch {
        Write-Host $_.Exception.Message -ForegroundColor Red
        $exitCode = 1
    }
    finally {
        Pop-Location
    }

    $results.Add([PSCustomObject]@{
        Name = $Name
        Passed = ($exitCode -eq 0)
        Seconds = [Math]::Round(((Get-Date) - $startedAt).TotalSeconds, 2)
    })

    if ($exitCode -eq 0) {
        Write-Host "<<< PASS: $Name" -ForegroundColor Green
    }
    else {
        Write-Host "<<< FAIL: $Name (exit $exitCode)" -ForegroundColor Red
    }
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    Stop-Preflight "poc\.venv\Scripts\python.exe was not found. Create the project virtual environment first."
}

$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npmCommand) {
    Stop-Preflight "npm.cmd was not found. Install Node.js 20 or newer."
}

if (-not (Test-Path -LiteralPath (Join-Path $appRoot "node_modules"))) {
    Stop-Preflight "app\node_modules was not found. Run npm install in app first."
}

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

Write-Host ""
Write-Host "========================================"
Write-Host " Meeting Copilot - M4 Local Regression"
Write-Host "========================================"
Write-Host "This suite is local-only: it does not call ASR/LLM services or use real meeting recordings."

$pythonTests = @(
    @{ Name = "Python / transcript turn splitting"; Module = "tests.test_turn_split" },
    @{ Name = "Python / adaptive me-speaker threshold"; Module = "tests.test_adaptive_cut" },
    @{ Name = "Python / offline transcript alignment"; Module = "tests.test_align_transcript" },
    @{ Name = "Python / offline me-cluster decision"; Module = "tests.test_diarize_decision" },
    @{ Name = "Python / speaker regression metrics"; Module = "tests.test_eval_speaker_regression" },
    @{ Name = "Python / PCM recording clock"; Module = "tests.test_audio_clock" },
    @{ Name = "Python / bridge timestamp mapping"; Module = "tests.test_bridge_timing" },
    @{ Name = "Python / meeting warmup and recording gate"; Module = "tests.test_meeting_startup" },
    @{ Name = "Python / Aliyun hotword vocabulary"; Module = "tests.test_asr_hotwords" },
    @{ Name = "Python / hotword real-recall scorer"; Module = "tests.test_hotword_recall" },
    @{ Name = "Python / long-meeting and timeline metrics"; Module = "tests.test_long_meeting_eval" },
    @{ Name = "Python / multi-track audio recorder"; Module = "tests.test_audio_recorder" },
    @{ Name = "Python / online system-only diarization"; Module = "tests.test_online_diarize" },
    @{ Name = "Python / diarization confidence downgrade"; Module = "tests.test_diarize_quality" },
    @{ Name = "Python / online dual-channel mixing"; Module = "tests.test_online_audio_stream" },
    @{ Name = "Python / DOCX and PDF extraction"; Module = "tests.test_document_extract" },
    @{ Name = "Python / suggestion evidence and quality"; Module = "tests.test_suggestion_quality" },
    @{ Name = "Python / LLM transient retry and diagnostics"; Module = "tests.test_llm_reliability" },
    @{ Name = "Python / dynamic LLM model catalog and fallback"; Module = "tests.test_service_check" },
    @{ Name = "Python / review enhancement timeout and diagnostics"; Module = "tests.test_review_reliability" },
    @{ Name = "Python / minutes structure and evidence prompt"; Module = "tests.test_minutes_quality" },
    @{ Name = "Python / scene prompt and minutes configuration"; Module = "tests.test_scene" },
    @{ Name = "Python / realtime scheduler coalescing"; Module = "tests.test_bridge_scheduler" }
)

foreach ($test in $pythonTests) {
    Invoke-RegressionStep `
        -Name $test.Name `
        -WorkingDirectory $pocRoot `
        -FilePath $pythonPath `
        -Arguments @("-m", $test.Module)
}

Invoke-RegressionStep `
    -Name "SQLite / legacy migration and persistence" `
    -WorkingDirectory $appRoot `
    -FilePath $npmCommand.Source `
    -Arguments @("run", "test:store")

Invoke-RegressionStep `
    -Name "Electron / warmup cache and delayed recording startup" `
    -WorkingDirectory $appRoot `
    -FilePath $npmCommand.Source `
    -Arguments @("run", "test:startup")

Invoke-RegressionStep `
    -Name "Electron renderer / playback timeline" `
    -WorkingDirectory $appRoot `
    -FilePath $npmCommand.Source `
    -Arguments @("run", "test:playback")

Invoke-RegressionStep `
    -Name "Electron renderer / timeline merge and speaker distribution" `
    -WorkingDirectory $appRoot `
    -FilePath $npmCommand.Source `
    -Arguments @("run", "test:timeline")

Invoke-RegressionStep `
    -Name "Electron renderer / TypeScript and Vite build" `
    -WorkingDirectory $appRoot `
    -FilePath $npmCommand.Source `
    -Arguments @("run", "build")

Write-Host ""
Write-Host "M4 regression summary" -ForegroundColor Cyan
Write-Host "---------------------"
foreach ($result in $results) {
    $status = if ($result.Passed) { "PASS" } else { "FAIL" }
    $color = if ($result.Passed) { "Green" } else { "Red" }
    Write-Host ("[{0}] {1} ({2:N2}s)" -f $status, $result.Name, $result.Seconds) -ForegroundColor $color
}

$failed = @($results | Where-Object { -not $_.Passed })
Write-Host ""
if ($failed.Count -gt 0) {
    Write-Host ("M4 REGRESSION FAILED: {0}/{1} checks failed." -f $failed.Count, $results.Count) -ForegroundColor Red
    exit 1
}

Write-Host ("M4 REGRESSION PASSED: {0}/{0} checks passed." -f $results.Count) -ForegroundColor Green
exit 0
