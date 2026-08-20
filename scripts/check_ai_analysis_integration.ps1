$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $projectRoot "compose.ai-analysis-check.yaml"

function Write-BoundedLines {
    param(
        [object[]]$Lines,
        [int]$Tail = 20
    )

    @($Lines) | Select-Object -Last $Tail | ForEach-Object {
        Write-Host ([string]$_)
    }
}

function Write-TestSummary {
    param([object[]]$Lines)

    $summary = @($Lines | Where-Object {
        ([string]$_) -match '^(FAIL:|\[[1-9]/10\]|\[10/10\]|AI-analysis fixture integration summary|  )'
    })
    if ($summary.Count -gt 0) {
        $summary | ForEach-Object { Write-Host ([string]$_) }
        return
    }
    Write-BoundedLines -Lines $Lines -Tail 15
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

Write-Host "AI Service Request Automation - AI-analysis fixture integration check"
Write-Host "Scope: 10 contract groups; fictional data; fixture provider; no Ollama call."
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

$env:AI_AUTOMATION_ANALYSIS_APP_DB_PASSWORD = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_ANALYSIS_APP_INTAKE_TOKEN = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_ANALYSIS_APP_SESSION_SECRET = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_ANALYSIS_APP_WORKFLOW_TOKEN = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)

$failureMessage = $null

try {
    $build = Invoke-NativeCapture {
        docker compose --file $composeFile build --quiet primary-api
    }
    if ($build.ExitCode -ne 0) {
        Write-BoundedLines -Lines $build.Output -Tail 20
        throw "The AI-analysis test image could not be prepared."
    }
    Write-Host "Application image: READY"

    $startup = Invoke-NativeCapture {
        docker compose --file $composeFile up `
            --wait `
            --wait-timeout 120 `
            primary-api
    }
    if ($startup.ExitCode -ne 0) {
        Write-BoundedLines -Lines $startup.Output -Tail 25
        throw "The AI-analysis services did not become healthy."
    }
    Write-Host "Disposable services: HEALTHY"

    $test = Invoke-NativeCapture {
        docker compose --file $composeFile run `
            --rm `
            --no-deps `
            tests
    }
    Write-TestSummary -Lines $test.Output

    if ($test.ExitCode -ne 0) {
        Write-Host ""
        Write-Host "Relevant service diagnostics:"
        $serviceLogs = Invoke-NativeCapture {
            docker compose --file $composeFile logs `
                --no-color `
                --tail 50 `
                primary-api
        }
        $relevantLogs = @($serviceLogs.Output | Where-Object {
            ([string]$_) -match '(?i)(traceback|exception|error|failed|POST /)'
        })
        if ($relevantLogs.Count -gt 0) {
            Write-BoundedLines -Lines $relevantLogs -Tail 20
        }
        else {
            Write-BoundedLines -Lines $serviceLogs.Output -Tail 12
        }
        throw "The AI-analysis fixture integration check failed. Return this short output to Codex."
    }

    Write-Host ""
    Write-Host "PASS: Deterministic AI-analysis passed its controlled fixture integration check."
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
    Remove-Item Env:\AI_AUTOMATION_ANALYSIS_APP_DB_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_ANALYSIS_APP_INTAKE_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_ANALYSIS_APP_SESSION_SECRET -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_ANALYSIS_APP_WORKFLOW_TOKEN -ErrorAction SilentlyContinue
}

if ($failureMessage) {
    Write-Host "FAIL: $failureMessage"
    exit 1
}
