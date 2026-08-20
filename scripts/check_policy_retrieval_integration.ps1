param([switch]$CleanupOnly)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $projectRoot "compose.policy-retrieval-check.yaml"

function Invoke-NativeCapture {
    param([scriptblock]$Command)
    $oldPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(& $Command 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldPreference
    }
    [pscustomobject]@{ Output = $output; ExitCode = $exitCode }
}

function Write-Tail {
    param([object[]]$Lines, [int]$Count = 18)
    @($Lines) | Select-Object -Last $Count | ForEach-Object { Write-Host ([string]$_) }
}

function Test-AcceptedModelsUnloaded {
    try {
        $running = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/ps" -TimeoutSec 10
        $acceptedNames = @(
            "qwen3-embedding:0.6b",
            "qwen3-embedding:0.6b:latest",
            "qwen3:4b-instruct",
            "qwen3:4b-instruct:latest"
        )
        return @($running.models | Where-Object { $_.name -in $acceptedNames }).Count -eq 0
    }
    catch {
        return $false
    }
}

function Invoke-ControlledCleanup {
    $dockerCleanup = Invoke-NativeCapture {
        docker compose --file $composeFile down --volumes --remove-orphans
    }

    # An already-unloaded model makes `ollama stop` write to stderr and return
    # nonzero. Capture that result, then judge cleanup from Ollama's actual
    # running-model state instead of treating the harmless stderr as failure.
    $null = Invoke-NativeCapture { ollama stop qwen3-embedding:0.6b }
    $null = Invoke-NativeCapture { ollama stop qwen3:4b-instruct }
    $modelsUnloaded = Test-AcceptedModelsUnloaded

    return [pscustomobject]@{
        DockerPassed = $dockerCleanup.ExitCode -eq 0
        DockerOutput = $dockerCleanup.Output
        ModelsUnloaded = $modelsUnloaded
        Passed = $dockerCleanup.ExitCode -eq 0 -and $modelsUnloaded
    }
}

function Remove-TemporaryEnvironment {
    Remove-Item Env:\AI_AUTOMATION_RETRIEVAL_DB_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_RETRIEVAL_INTAKE_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_RETRIEVAL_SESSION_SECRET -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_RETRIEVAL_WORKFLOW_TOKEN -ErrorAction SilentlyContinue
}

if ($CleanupOnly) {
    Write-Host "AI Service Request Automation - policy-retrieval cleanup verification"
    Write-Host "Scope: existing disposable Docker state and accepted local model state only."
}
else {
    Write-Host "AI Service Request Automation - policy-retrieval integration check"
    Write-Host "Scope: 6 focused groups; fictional data; installed local models only."
    Write-Host "No model download and no hosted or paid AI call."
}
Write-Host ""

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "FAIL: Docker was not found. Install and start Docker Desktop first."
    exit 1
}
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Host "FAIL: Ollama was not found on PATH."
    exit 1
}
$dockerInfo = Invoke-NativeCapture { docker info }
if ($dockerInfo.ExitCode -ne 0) {
    Write-Host "FAIL: Docker Engine is not running."
    exit 1
}
try {
    $tags = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 10
    $embedding = @($tags.models | Where-Object { $_.name -in @("qwen3-embedding:0.6b", "qwen3-embedding:0.6b:latest") })[0]
    $answer = @($tags.models | Where-Object { $_.name -in @("qwen3:4b-instruct", "qwen3:4b-instruct:latest") })[0]
    if (-not $embedding -or -not ([string]$embedding.digest).StartsWith("ac6da0dfba84")) {
        throw "qwen3-embedding:0.6b is missing or changed"
    }
    if (-not $answer -or -not ([string]$answer.digest).StartsWith("0edcdef34593")) {
        throw "qwen3:4b-instruct is missing or changed"
    }
}
catch {
    Write-Host "FAIL: Accepted installed Ollama models are unavailable. Start Ollama; this runner will not download them."
    exit 1
}
Write-Host "Accepted local models: READY"

$env:AI_AUTOMATION_RETRIEVAL_DB_PASSWORD = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$env:AI_AUTOMATION_RETRIEVAL_INTAKE_TOKEN = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$env:AI_AUTOMATION_RETRIEVAL_SESSION_SECRET = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$env:AI_AUTOMATION_RETRIEVAL_WORKFLOW_TOKEN = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")

if ($CleanupOnly) {
    $cleanupOnlyResult = Invoke-ControlledCleanup
    Remove-TemporaryEnvironment
    if ($cleanupOnlyResult.Passed) {
        Write-Host "Docker state absent: PASS"
        Write-Host "Accepted local models unloaded: PASS"
        Write-Host "Cleanup-only verification: PASS"
        exit 0
    }
    if (-not $cleanupOnlyResult.DockerPassed) {
        Write-Tail $cleanupOnlyResult.DockerOutput 12
    }
    Write-Host "Cleanup-only verification: FAIL"
    exit 1
}

$failure = $null

try {
    $build = Invoke-NativeCapture { docker compose --file $composeFile build --quiet primary-api }
    if ($build.ExitCode -ne 0) {
        Write-Tail $build.Output
        throw "The policy-retrieval application image could not be prepared."
    }
    Write-Host "Application image: READY"
    $startup = Invoke-NativeCapture { docker compose --file $composeFile up --wait --wait-timeout 120 primary-api }
    if ($startup.ExitCode -ne 0) {
        Write-Tail $startup.Output 22
        throw "The disposable retrieval services did not become healthy."
    }
    Write-Host "Disposable services: HEALTHY"
    $tests = Invoke-NativeCapture { docker compose --file $composeFile run --rm --no-deps tests }
    if ($tests.ExitCode -ne 0) {
        Write-Tail $tests.Output 24
        throw "The policy-retrieval integration groups failed."
    }
    if (-not ($tests.Output -match '^  Policy-retrieval gate: PASS$')) {
        throw "The policy-retrieval completion marker was missing."
    }
    @($tests.Output | Where-Object {
        ([string]$_) -match '^(\[[1-6]/6\]|Policy-retrieval integration summary|  )'
    }) | ForEach-Object { Write-Host ([string]$_) }
    Write-Host ""
    Write-Host "PASS: Policy retrieval passed its controlled local application check."
}
catch {
    $failure = $_.Exception.Message
    Write-Host ""
    Write-Host "Relevant service diagnostics:"
    $logs = Invoke-NativeCapture { docker compose --file $composeFile logs --no-color --tail 40 primary-api }
    $relevant = @($logs.Output | Where-Object { ([string]$_) -match '(?i)(traceback|exception|error|failed|POST /)' })
    Write-Tail ($(if ($relevant.Count) { $relevant } else { $logs.Output })) 16
}
finally {
    $cleanup = Invoke-ControlledCleanup
    if ($cleanup.Passed) {
        Write-Host "Cleanup: PASS"
    }
    else {
        Write-Host "Cleanup: FAIL"
        if (-not $cleanup.DockerPassed) {
            Write-Tail $cleanup.DockerOutput 12
        }
        if (-not $cleanup.ModelsUnloaded) {
            Write-Host "Accepted local model unload verification failed."
        }
        if (-not $failure) {
            $failure = "Disposable Docker or local-model cleanup failed."
        }
    }
    Remove-TemporaryEnvironment
}
if ($failure) {
    Write-Host "FAIL: $failure"
    exit 1
}
