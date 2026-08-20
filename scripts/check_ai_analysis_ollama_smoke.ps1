$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $projectRoot "compose.ai-analysis-ollama-smoke.yaml"
$ollamaBaseUrl = "http://127.0.0.1:11434"
$modelName = "qwen3:4b-instruct"
$acceptedModelIdPrefix = "0edcdef34593"

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
        ([string]$_) -match '^(FAIL:|\[[12]/2\]|AI-analysis Ollama smoke summary|  )'
    })
    if ($summary.Count -gt 0) {
        $summary | ForEach-Object { Write-Host ([string]$_) }
        return
    }
    Write-BoundedLines -Lines $Lines -Tail 12
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

Write-Host "AI Service Request Automation - AI-analysis Ollama smoke check"
Write-Host "Scope: 2 existing benchmark cases; 1 English, 1 Indonesian; local Ollama only."
Write-Host "No model download and no paid or hosted AI call."
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

try {
    $tags = Invoke-RestMethod -Method Get -Uri "$ollamaBaseUrl/api/tags" -TimeoutSec 10
}
catch {
    Write-Host "FAIL: Local Ollama is not reachable at $ollamaBaseUrl. Start Ollama first."
    exit 1
}

$model = @($tags.models | Where-Object {
    $_.name -eq $modelName -or $_.name -eq "${modelName}:latest"
}) | Select-Object -First 1
if (-not $model) {
    Write-Host "FAIL: $modelName is not installed. This runner will not download it."
    exit 1
}
$modelIdentifier = [string]$model.digest
if (
    $modelIdentifier -notmatch '^[0-9a-f]{64}$' -or
    -not $modelIdentifier.StartsWith($acceptedModelIdPrefix)
) {
    Write-Host "FAIL: The installed model digest does not match the accepted suitability evidence."
    Write-Host "Expected ID prefix: $acceptedModelIdPrefix"
    exit 1
}
Write-Host "Accepted local model: READY"

$env:AI_AUTOMATION_OLLAMA_SMOKE_DB_PASSWORD = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_OLLAMA_SMOKE_INTAKE_TOKEN = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_OLLAMA_SMOKE_SESSION_SECRET = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_OLLAMA_SMOKE_WORKFLOW_TOKEN = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_OLLAMA_SMOKE_MODEL_IDENTIFIER = $modelIdentifier

$failureMessage = $null

try {
    $build = Invoke-NativeCapture {
        docker compose --file $composeFile build --quiet primary-api
    }
    if ($build.ExitCode -ne 0) {
        Write-BoundedLines -Lines $build.Output -Tail 20
        throw "The Ollama smoke application image could not be prepared."
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
        throw "The disposable smoke services did not become healthy."
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
        throw "The 2-case Ollama smoke check failed. Return this short output to Codex."
    }

    Write-Host ""
    Write-Host "PASS: The local Ollama adapter passed its focused application smoke check."
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
    Remove-Item Env:\AI_AUTOMATION_OLLAMA_SMOKE_DB_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_OLLAMA_SMOKE_INTAKE_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_OLLAMA_SMOKE_SESSION_SECRET -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_OLLAMA_SMOKE_WORKFLOW_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_OLLAMA_SMOKE_MODEL_IDENTIFIER -ErrorAction SilentlyContinue
}

if ($failureMessage) {
    Write-Host "FAIL: $failureMessage"
    exit 1
}
