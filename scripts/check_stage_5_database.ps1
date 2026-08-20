$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $projectRoot "compose.stage5-check.yaml"

Write-Host "AI Service Request Automation - stage 5 primary database check"
Write-Host "This check rebuilds stages 1 through 5 with fictional data."
Write-Host "It validates the outbox and delivery-attempt foundation only."
Write-Host "No downstream API, worker, orchestration, or AI model is called."
Write-Host ""

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker was not found. Install and start Docker Desktop first."
}

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Engine is not running. Open Docker Desktop and wait until the engine is ready."
}

$env:AI_AUTOMATION_STAGE5_DB_PASSWORD = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)

try {
    docker compose --file $composeFile up `
        --abort-on-container-exit `
        --exit-code-from tests

    if ($LASTEXITCODE -ne 0) {
        throw "The stage 5 database check failed. Return the terminal output to Codex."
    }

    Write-Host ""
    Write-Host "PASS: Outbox idempotency, retries, and delivery evidence passed their runtime checks."
}
finally {
    docker compose --file $composeFile down
    Remove-Item Env:\AI_AUTOMATION_STAGE5_DB_PASSWORD -ErrorAction SilentlyContinue
}
