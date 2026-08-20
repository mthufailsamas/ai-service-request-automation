$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $PSScriptRoot "compose.yaml"
$environmentFile = Join-Path $projectRoot ".runtime\guided-demo.env"
$modelUseMarker = Join-Path $projectRoot ".runtime\guided-demo-ollama-active"

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

Write-Host "AI Service Request Automation - guided Ollama lesson"
Write-Host "Scope: 1 fictional request; installed local model only; no download."
Write-Host ""

if (-not (Test-Path -LiteralPath $environmentFile)) {
    Write-Host "FAIL: Start the guided demo first with 'demo.cmd start'."
    exit 1
}
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Host "FAIL: Ollama was not found."
    exit 1
}

$modelList = Invoke-NativeCapture { ollama list }
if (
    $modelList.ExitCode -ne 0 -or
    ($modelList.Output -join "`n") -notmatch "qwen3:4b-instruct\s+0edcdef34593"
) {
    Write-Host "FAIL: The accepted qwen3:4b-instruct model is not installed."
    Write-Host "No model was downloaded."
    exit 1
}

$services = Invoke-NativeCapture {
    docker compose --env-file $environmentFile --file $composeFile ps --status running --services
}
if (
    $services.ExitCode -ne 0 -or
    ($services.Output -notcontains "database") -or
    ($services.Output -notcontains "primary-api")
) {
    Write-Host "FAIL: The guided demo is not healthy. Run 'demo.cmd start' first."
    exit 1
}

$failure = $null
try {
    Set-Content -LiteralPath $modelUseMarker -Value "qwen3:4b-instruct" -Encoding ascii
    $execution = Invoke-NativeCapture {
        docker compose --env-file $environmentFile --file $composeFile run --rm --no-deps ollama-case
    }
    if ($execution.ExitCode -ne 0) {
        @($execution.Output) | Select-Object -Last 18 | ForEach-Object {
            Write-Host ([string]$_)
        }
        throw "The guided Ollama case did not complete."
    }
    @($execution.Output | Where-Object {
        ([string]$_) -match '^(AI Service|Scope:|Case:|Provider called:|Request type:|State:|AI summary:|Open http)'
    }) | ForEach-Object { Write-Host ([string]$_) }
}
catch {
    $failure = $_.Exception.Message
}
finally {
    $stopResult = Invoke-NativeCapture { ollama stop qwen3:4b-instruct }
    $runningModels = Invoke-NativeCapture { ollama ps }
    if (
        $runningModels.ExitCode -eq 0 -and
        ($runningModels.Output -join "`n") -notmatch "qwen3:4b-instruct"
    ) {
        Remove-Item -LiteralPath $modelUseMarker -ErrorAction SilentlyContinue
        Write-Host "Accepted local model unloaded: PASS"
    }
    else {
        Write-Host "Accepted local model unloaded: FAIL"
        if (-not $failure) {
            $failure = "The guided Ollama model remained loaded."
        }
    }
}

if ($failure) {
    Write-Host "FAIL: $failure"
    exit 1
}
