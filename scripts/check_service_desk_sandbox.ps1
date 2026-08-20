$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $projectRoot "compose.sandbox-check.yaml"

Write-Host "AI Service Request Automation - Service Desk Sandbox check"
Write-Host "This check builds 1 local API and its isolated 2-table PostgreSQL database."
Write-Host "It uses fictional fixtures and temporary credentials."
Write-Host "No primary application, n8n workflow, external service, or AI model is called."
Write-Host ""

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker was not found. Install and start Docker Desktop first."
}

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Engine is not running. Open Docker Desktop and wait until the engine is ready."
}

$env:AI_AUTOMATION_SANDBOX_DB_PASSWORD = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_SANDBOX_TOKEN = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)

try {
    docker compose --file $composeFile up `
        --build `
        --abort-on-container-exit `
        --exit-code-from tests

    if ($LASTEXITCODE -ne 0) {
        throw "The Service Desk Sandbox check failed. Return the terminal output to Codex."
    }

    Write-Host ""
    Write-Host "PASS: The accepted Service Desk Sandbox contract passed its runtime check."
}
finally {
    docker compose --file $composeFile down --volumes --remove-orphans
    Remove-Item Env:\AI_AUTOMATION_SANDBOX_DB_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_SANDBOX_TOKEN -ErrorAction SilentlyContinue
}
