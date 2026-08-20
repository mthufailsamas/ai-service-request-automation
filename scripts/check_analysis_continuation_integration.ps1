$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $projectRoot "compose.analysis-continuation-check.yaml"

function Write-BoundedLines {
    param(
        [object[]]$Lines,
        [int]$Tail = 20
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

Write-Host "AI Service Request Automation - analysis-continuation integration check"
Write-Host "Scope: 5 focused groups; fictional data; fixture provider; 0 Ollama calls."
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

$env:AI_AUTOMATION_CONTINUATION_DB_PASSWORD = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_CONTINUATION_INTAKE_TOKEN = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_CONTINUATION_SESSION_SECRET = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_CONTINUATION_PRIMARY_TOKEN = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_CONTINUATION_N8N_TOKEN = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_CONTINUATION_N8N_ENCRYPTION_KEY = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)

$failureMessage = $null

try {
    $build = Invoke-NativeCapture {
        docker compose --file $composeFile build --quiet primary-api
    }
    if ($build.ExitCode -ne 0) {
        Write-BoundedLines -Lines $build.Output -Tail 20
        throw "The continuation application image could not be prepared."
    }
    Write-Host "Application image: READY"

    $startup = Invoke-NativeCapture {
        docker compose --file $composeFile up `
            --wait `
            --wait-timeout 120 `
            primary-api `
            n8n
    }
    if ($startup.ExitCode -ne 0) {
        Write-BoundedLines -Lines $startup.Output -Tail 25
        throw "The disposable continuation services did not become healthy."
    }
    Write-Host "Disposable services: HEALTHY"

    $integration = Invoke-NativeCapture {
        docker compose --file $composeFile run `
            --rm `
            --no-deps `
            tests
    }
    if ($integration.ExitCode -ne 0) {
        Write-BoundedLines -Lines $integration.Output -Tail 20
        throw "The continuation runtime groups failed."
    }
    if (-not ($integration.Output -match '^RUNTIME_RETRYABLE_CLASSIFICATION: PASS$')) {
        throw "The retryable continuation runtime marker was missing."
    }

    $classifier = Invoke-NativeCapture {
        docker compose --file $composeFile run `
            --rm `
            --no-deps `
            classifier-tests
    }
    if ($classifier.ExitCode -ne 0) {
        Write-BoundedLines -Lines $classifier.Output -Tail 12
        throw "The bounded analysis-response classifier fixtures failed."
    }
    if (-not ($classifier.Output -match '^\[4/5\] Retryable and malformed response classification: PASS$')) {
        throw "The bounded analysis-response classifier marker was missing."
    }

    @($integration.Output | Where-Object {
        ([string]$_) -match '^\[[1-3]/5\]'
    }) | ForEach-Object { Write-Host ([string]$_) }
    @($classifier.Output | Where-Object {
        ([string]$_) -match '^\[4/5\]'
    }) | ForEach-Object { Write-Host ([string]$_) }
    @($integration.Output | Where-Object {
        ([string]$_) -match '^(\[5/5\]|Analysis-continuation integration summary|  )'
    }) | ForEach-Object { Write-Host ([string]$_) }

    Write-Host ""
    Write-Host "PASS: Post-response analysis continuation passed its controlled fixture integration check."
}
catch {
    $failureMessage = $_.Exception.Message
    Write-Host ""
    Write-Host "Relevant service diagnostics:"
    $serviceLogs = Invoke-NativeCapture {
        docker compose --file $composeFile logs `
            --no-color `
            --tail 50 `
            primary-api `
            n8n
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
    Remove-Item Env:\AI_AUTOMATION_CONTINUATION_DB_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_CONTINUATION_INTAKE_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_CONTINUATION_SESSION_SECRET -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_CONTINUATION_PRIMARY_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_CONTINUATION_N8N_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_CONTINUATION_N8N_ENCRYPTION_KEY -ErrorAction SilentlyContinue
}

if ($failureMessage) {
    Write-Host "FAIL: $failureMessage"
    exit 1
}
