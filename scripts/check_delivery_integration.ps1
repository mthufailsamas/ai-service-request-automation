$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $projectRoot "compose.delivery-check.yaml"

Write-Host "AI Service Request Automation - primary delivery integration check"
Write-Host "This check connects the primary outbox worker to the local Service Desk Sandbox."
Write-Host "It uses 2 isolated temporary databases, fictional fixtures, and temporary credentials."
Write-Host "No n8n workflow, external service, requester notification, or AI model is called."
Write-Host ""

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker was not found. Install and start Docker Desktop first."
}

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Engine is not running. Open Docker Desktop and wait until the engine is ready."
}

$env:AI_AUTOMATION_DELIVERY_PRIMARY_DB_PASSWORD = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_DELIVERY_SANDBOX_DB_PASSWORD = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_DELIVERY_SANDBOX_TOKEN = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)

try {
    docker compose --file $composeFile up `
        --build `
        --abort-on-container-exit `
        --exit-code-from tests

    if ($LASTEXITCODE -ne 0) {
        throw "The primary delivery integration check failed. Return the terminal output to Codex."
    }

    Write-Host ""
    Write-Host "PASS: The primary delivery integration passed its controlled runtime check."
}
finally {
    docker compose --file $composeFile down --volumes --remove-orphans
    Remove-Item Env:\AI_AUTOMATION_DELIVERY_PRIMARY_DB_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_DELIVERY_SANDBOX_DB_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_DELIVERY_SANDBOX_TOKEN -ErrorAction SilentlyContinue
}
