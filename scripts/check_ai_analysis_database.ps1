$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $projectRoot "compose.ai-analysis-database-check.yaml"

function Write-BoundedLines {
    param(
        [object[]]$Lines,
        [int]$Tail = 25
    )

    @($Lines) | Select-Object -Last $Tail | ForEach-Object {
        Write-Host ([string]$_)
    }
}

function Invoke-NativeCapture {
    param([scriptblock]$Command)

    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(& $Command 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }

    return [pscustomobject]@{
        Output = $output
        ExitCode = $exitCode
    }
}

Write-Host "AI Service Request Automation - AI-analysis database check"
Write-Host "Scope: 1 additive schema/lifecycle check; fictional data; disposable PostgreSQL; no AI call."
Write-Host ""

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "FAIL: Docker was not found. Install and start Docker Desktop first."
    exit 1
}

$dockerInfo = Invoke-NativeCapture { docker info }
if ($dockerInfo.ExitCode -ne 0) {
    Write-Host "FAIL: Docker Engine is not running. Open Docker Desktop and wait until it is ready."
    exit 1
}

$env:AI_AUTOMATION_ANALYSIS_DB_PASSWORD = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)

$failureMessage = $null

try {
    $test = Invoke-NativeCapture {
        docker compose --file $composeFile up `
            --abort-on-container-exit `
            --exit-code-from tests `
            --quiet-pull
    }

    if ($test.ExitCode -ne 0) {
        Write-BoundedLines -Lines $test.Output -Tail 30
        throw "The AI-analysis database check failed. Return this short terminal output to Codex."
    }

    if (-not ($test.Output -match 'PASS: AI-analysis runtime database foundation checks')) {
        Write-BoundedLines -Lines $test.Output -Tail 20
        throw "The database check exited without its required PASS marker."
    }

    Write-Host "Schema and lifecycle constraints: PASS"
    Write-Host "ANALYZING to FAILED evidence: PASS"
    Write-Host "Bounded attempts and SKIPPED outcome: PASS"
    Write-Host "PASS: AI-analysis persistence foundation passed its controlled database check."
}
catch {
    $failureMessage = $_.Exception.Message
}
finally {
    $cleanup = Invoke-NativeCapture {
        docker compose --file $composeFile down `
            --volumes `
            --remove-orphans
    }
    if ($cleanup.ExitCode -eq 0) {
        Write-Host "Cleanup: PASS"
    }
    else {
        Write-Host "Cleanup: FAIL"
        Write-BoundedLines -Lines $cleanup.Output -Tail 12
        if (-not $failureMessage) {
            $failureMessage = "Disposable Docker cleanup failed."
        }
    }
    Remove-Item Env:\AI_AUTOMATION_ANALYSIS_DB_PASSWORD -ErrorAction SilentlyContinue
}

if ($failureMessage) {
    Write-Host "FAIL: $failureMessage"
    exit 1
}
