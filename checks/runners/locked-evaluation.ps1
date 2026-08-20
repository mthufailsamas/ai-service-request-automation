$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$composeFile = Join-Path $projectRoot "checks\compose\locked-evaluation.yaml"
$corpusFile = Join-Path $projectRoot "evaluation\locked_system_evaluation_v1.json"
$evidenceDirectory = Join-Path $projectRoot "output\locked_evaluation"
$evidenceFile = Join-Path $evidenceDirectory "locked-system-evaluation-v1.json"
$expectedCorpusSha256 = "17e6806295cba62a519353c7db4396eefbc0e2e07a999972520ef009b6477354"
$expectedPartialEvidenceSha256 = "a1cd541f9a1abff288fe7877319d8cc5da3a45b7b8921957da1afcaa90968b39"
$resumeFromPartial = $false

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
    param([object[]]$Lines, [int]$Count = 20)
    @($Lines) | Select-Object -Last $Count | ForEach-Object {
        Write-Host ([string]$_)
    }
}

function Invoke-ApplicationBuild {
    $result = $null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $result = Invoke-NativeCapture {
            docker compose --project-directory $projectRoot --file $composeFile build --quiet tests sandbox-api
        }
        if ($result.ExitCode -eq 0) {
            return $result
        }
        $message = @($result.Output) -join "`n"
        $metadataLock = $message -match '(?is)no valid drivers found.*failed to read metadata.*being used by another process'
        if (-not $metadataLock -or $attempt -eq 3) {
            return $result
        }
        Start-Sleep -Seconds $attempt
    }
    return $result
}

function Test-AcceptedModelsUnloaded {
    try {
        $running = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/ps" -TimeoutSec 10
        $accepted = @(
            "qwen3-embedding:0.6b",
            "qwen3-embedding:0.6b:latest",
            "qwen3:4b-instruct",
            "qwen3:4b-instruct:latest"
        )
        return @($running.models | Where-Object { $_.name -in $accepted }).Count -eq 0
    }
    catch {
        return $false
    }
}

function Wait-AcceptedModelsUnloaded {
    for ($attempt = 1; $attempt -le 20; $attempt++) {
        if (Test-AcceptedModelsUnloaded) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Invoke-ControlledCleanup {
    $dockerCleanup = Invoke-NativeCapture {
        docker compose --project-directory $projectRoot --file $composeFile down --volumes --remove-orphans
    }
    $null = Invoke-NativeCapture { ollama stop qwen3-embedding:0.6b }
    $null = Invoke-NativeCapture { ollama stop qwen3:4b-instruct }
    $modelsUnloaded = Wait-AcceptedModelsUnloaded
    return [pscustomobject]@{
        DockerPassed = $dockerCleanup.ExitCode -eq 0
        DockerOutput = $dockerCleanup.Output
        ModelsUnloaded = $modelsUnloaded
        Passed = $dockerCleanup.ExitCode -eq 0 -and $modelsUnloaded
    }
}

function Remove-TemporaryEnvironment {
    @(
        "AI_AUTOMATION_EVAL_PRIMARY_DB_PASSWORD",
        "AI_AUTOMATION_EVAL_SANDBOX_DB_PASSWORD",
        "AI_AUTOMATION_EVAL_SANDBOX_TOKEN",
        "AI_AUTOMATION_EVAL_ANALYSIS_IDENTIFIER",
        "AI_AUTOMATION_EVAL_EMBEDDING_IDENTIFIER",
        "AI_AUTOMATION_EVAL_CORPUS_SHA256"
    ) | ForEach-Object {
        Remove-Item "Env:\$_" -ErrorAction SilentlyContinue
    }
}

Write-Host "AI Service Request Automation - locked 50-case system evaluation"
Write-Host "Scope: 40 new semantic cases; 10 workflow controls; installed local models only."
Write-Host "No model download and no hosted or paid AI call."
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

$actualCorpusSha256 = (Get-FileHash -LiteralPath $corpusFile -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualCorpusSha256 -ne $expectedCorpusSha256) {
    Write-Host "FAIL: The locked evaluation corpus changed before execution."
    exit 1
}

if (Test-Path -LiteralPath $evidenceFile) {
    try {
        $existingEvidence = Get-Content -LiteralPath $evidenceFile -Raw | ConvertFrom-Json
    }
    catch {
        Write-Host "FAIL: Existing locked-evaluation evidence is unreadable; preserve it for audit and return this message to Codex."
        exit 1
    }
    if ($existingEvidence.status -eq "USER_EXECUTED_CONTROLLED_EVALUATION") {
        Write-Host "FAIL: Completed locked-evaluation evidence already exists; the accepted evaluation must not be rerun."
        exit 1
    }
    $resumeFromPartial = (
        $existingEvidence.status -eq "USER_EXECUTED_PARTIAL_EVALUATION" -and
        $existingEvidence.completed_boundary -eq "SEMANTIC_AND_RETRIEVAL"
    )
    if ($resumeFromPartial) {
        $actualPartialEvidenceSha256 = (Get-FileHash -LiteralPath $evidenceFile -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualPartialEvidenceSha256 -ne $expectedPartialEvidenceSha256) {
            Write-Host "FAIL: Preserved semantic evidence changed after review; do not rerun the model workload."
            exit 1
        }
    }
}

try {
    $tags = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 10
    $analysis = @($tags.models | Where-Object {
        $_.name -in @("qwen3:4b-instruct", "qwen3:4b-instruct:latest")
    })[0]
    $embedding = @($tags.models | Where-Object {
        $_.name -in @("qwen3-embedding:0.6b", "qwen3-embedding:0.6b:latest")
    })[0]
    if (-not $analysis -or -not ([string]$analysis.digest).StartsWith("0edcdef34593")) {
        throw "the accepted analysis model is missing or changed"
    }
    if (-not $embedding -or -not ([string]$embedding.digest).StartsWith("ac6da0dfba84")) {
        throw "the accepted embedding model is missing or changed"
    }
}
catch {
    Write-Host "FAIL: Accepted installed Ollama models are unavailable. Start Ollama; this runner will not download them."
    exit 1
}
Write-Host "Locked corpus and accepted local models: READY"
if ($resumeFromPartial) {
    Write-Host "Preserved semantic and retrieval evidence: READY; model rerun disabled"
}

$env:AI_AUTOMATION_EVAL_PRIMARY_DB_PASSWORD = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$env:AI_AUTOMATION_EVAL_SANDBOX_DB_PASSWORD = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$env:AI_AUTOMATION_EVAL_SANDBOX_TOKEN = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$env:AI_AUTOMATION_EVAL_ANALYSIS_IDENTIFIER = [string]$analysis.digest
$env:AI_AUTOMATION_EVAL_EMBEDDING_IDENTIFIER = [string]$embedding.digest
$env:AI_AUTOMATION_EVAL_CORPUS_SHA256 = $actualCorpusSha256
$null = New-Item -ItemType Directory -Path $evidenceDirectory -Force
$failure = $null
$gate = $null

try {
    $resolvedConfig = Invoke-NativeCapture {
        docker compose --project-directory $projectRoot --file $composeFile config --format json
    }
    if ($resolvedConfig.ExitCode -ne 0) {
        Write-Tail $resolvedConfig.Output
        throw "The locked-evaluation Compose contract is invalid."
    }
    $configObject = (@($resolvedConfig.Output) -join "`n") | ConvertFrom-Json
    $invalidTmpfs = @(
        $configObject.services.psobject.Properties.Value.tmpfs |
            Where-Object { $_ -and -not ([string]$_).StartsWith("/") }
    )
    if ($invalidTmpfs.Count -gt 0) {
        throw "The locked-evaluation Compose contract contains an invalid tmpfs path."
    }

    $build = Invoke-ApplicationBuild
    if ($build.ExitCode -ne 0) {
        Write-Tail $build.Output
        throw "The locked-evaluation application images could not be prepared."
    }
    Write-Host "Application images: READY"

    $startup = Invoke-NativeCapture {
        docker compose --project-directory $projectRoot --file $composeFile up --wait --wait-timeout 150 primary-database sandbox-api
    }
    if ($startup.ExitCode -ne 0) {
        Write-Tail $startup.Output 22
        throw "The disposable evaluation services did not become healthy."
    }
    Write-Host "Disposable services: HEALTHY"

    $tests = Invoke-NativeCapture {
        docker compose --project-directory $projectRoot --file $composeFile run --rm --no-deps tests
    }
    if ($tests.ExitCode -ne 0) {
        Write-Tail $tests.Output 30
        throw "The locked 50-case evaluation did not complete."
    }
    $gateLine = @($tests.Output | Where-Object {
        ([string]$_) -match '^  Locked evaluation gate: (PASS|CHECK)$'
    }) | Select-Object -Last 1
    if (-not $gateLine) {
        throw "The locked-evaluation completion marker was missing."
    }
    $gate = if (([string]$gateLine) -match 'PASS$') { "PASS" } else { "CHECK" }
    @($tests.Output | Where-Object {
        ([string]$_) -match '^(\[[1-5]/5\]|Locked 50-case evaluation summary|  )'
    }) | ForEach-Object { Write-Host ([string]$_) }
    Write-Host ""
    if ($gate -eq "PASS") {
        Write-Host "PASS: The locked local evaluation met every fixed acceptance target."
    }
    else {
        Write-Host "CHECK: The locked evaluation completed, but at least one fixed quality target was not met."
    }
}
catch {
    $failure = $_.Exception.Message
    Write-Host ""
    Write-Host "Relevant service diagnostics:"
    $logs = Invoke-NativeCapture {
        docker compose --project-directory $projectRoot --file $composeFile logs --no-color --tail 50 sandbox-api
    }
    $relevant = @($logs.Output | Where-Object {
        ([string]$_) -match '(?i)(traceback|exception|error|failed|POST /)'
    })
    Write-Tail $(if ($relevant.Count) { $relevant } else { $logs.Output }) 18
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
