$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $projectRoot "compose.intake-check.yaml"

Write-Host "AI Service Request Automation - primary intake integration check"
Write-Host "This check builds the accepted web and REST-webhook intake boundary."
Write-Host "It uses 1 isolated temporary database, fictional data, and temporary credentials."
Write-Host "No n8n workflow, Ollama model, policy retrieval, or downstream service is called."
Write-Host ""

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker was not found. Install and start Docker Desktop first."
}

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Engine is not running. Open Docker Desktop and wait until the engine is ready."
}

$env:AI_AUTOMATION_INTAKE_DB_PASSWORD = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_INTAKE_WEBHOOK_TOKEN = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_INTAKE_SESSION_SECRET = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_INTAKE_WORKFLOW_TOKEN = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)

try {
    docker compose --file $composeFile up `
        --build `
        --abort-on-container-exit `
        --exit-code-from tests

    if ($LASTEXITCODE -ne 0) {
        throw "The primary intake integration check failed. Return the terminal output to Codex."
    }

    Write-Host ""
    Write-Host "PASS: Primary intake passed its controlled local runtime check."
}
finally {
    docker compose --file $composeFile down --volumes --remove-orphans
    Remove-Item Env:\AI_AUTOMATION_INTAKE_DB_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_INTAKE_WEBHOOK_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_INTAKE_SESSION_SECRET -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_INTAKE_WORKFLOW_TOKEN -ErrorAction SilentlyContinue
}
