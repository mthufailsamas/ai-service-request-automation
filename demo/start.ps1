$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $PSScriptRoot "compose.yaml"
$runtimeDirectory = Join-Path $projectRoot ".runtime"
$environmentFile = Join-Path $runtimeDirectory "guided-demo.env"

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
    param([object[]]$Lines, [int]$Count = 16)
    @($Lines) | Select-Object -Last $Count | ForEach-Object {
        Write-Host ([string]$_)
    }
}

function New-TemporarySecret {
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
}

Write-Host "AI Service Request Automation - guided local demo"
Write-Host "Scope: 3 human-decision cases + 1 reserved Ollama case; 0 AI calls at startup."
Write-Host ""

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "FAIL: Docker was not found. Install and start Docker Desktop first."
    exit 1
}
$dockerInfo = Invoke-NativeCapture { docker info }
if ($dockerInfo.ExitCode -ne 0) {
    Write-Host "FAIL: Docker Engine is not running."
    exit 1
}

New-Item -ItemType Directory -Force -Path $runtimeDirectory | Out-Null
if (-not (Test-Path -LiteralPath $environmentFile)) {
    @(
        "AI_AUTOMATION_DEMO_DB_PASSWORD=$(New-TemporarySecret)"
        "AI_AUTOMATION_DEMO_INTAKE_TOKEN=$(New-TemporarySecret)"
        "AI_AUTOMATION_DEMO_SESSION_SECRET=$(New-TemporarySecret)"
        "AI_AUTOMATION_DEMO_WORKFLOW_TOKEN=$(New-TemporarySecret)"
    ) | Set-Content -LiteralPath $environmentFile -Encoding ascii
}

$failure = $null
try {
    $build = Invoke-NativeCapture {
        docker compose --env-file $environmentFile --file $composeFile build --quiet primary-api
    }
    if ($build.ExitCode -ne 0) {
        Write-Tail $build.Output
        throw "The guided demo application image could not be prepared."
    }
    Write-Host "Application image: READY"

    $startup = Invoke-NativeCapture {
        docker compose --env-file $environmentFile --file $composeFile up --detach --no-build --wait --wait-timeout 120 primary-api
    }
    if ($startup.ExitCode -ne 0) {
        Write-Tail $startup.Output 22
        throw "The guided demo services did not become healthy."
    }
    Write-Host "Guided demo services: HEALTHY"

    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 5
    if ($health.status -ne "ok") {
        throw "The guided demo health response was invalid."
    }

    $ollamaReady = $false
    if (Get-Command ollama -ErrorAction SilentlyContinue) {
        $modelList = Invoke-NativeCapture { ollama list }
        $ollamaReady = (
            $modelList.ExitCode -eq 0 -and
            ($modelList.Output -join "`n") -match "qwen3:4b-instruct\s+0edcdef34593"
        )
    }

    Write-Host "Portal health: PASS"
    Write-Host "Ollama lesson: $(if ($ollamaReady) { 'READY' } else { 'UNAVAILABLE; no model was downloaded' })"
    Write-Host ""
    Write-Host "Open: http://127.0.0.1:8000/login"
    Write-Host "Password for every fictional account: Demo-Local-Only-2026!"
    Write-Host "  Requester: EMP-201"
    Write-Host "  Service agent: AGT-301"
    Write-Host "  Approver: MGR-104"
    Write-Host "  Administrator: ADM-001"
    Write-Host ""
    Write-Host "Next: follow demo\README.md. Run 'demo.cmd ollama' only at its Ollama lesson."
    Write-Host "Stop and clean up with: demo.cmd stop"
}
catch {
    $failure = $_.Exception.Message
    Write-Host ""
    Write-Host "Relevant diagnostics:"
    $setupLogs = Invoke-NativeCapture {
        docker compose --env-file $environmentFile --file $composeFile logs --no-color --tail 30 setup
    }
    $primaryLogs = Invoke-NativeCapture {
        docker compose --env-file $environmentFile --file $composeFile logs --no-color --tail 30 primary-api
    }
    if (@($setupLogs.Output).Count -gt 0) {
        Write-Tail $setupLogs.Output 14
    }
    if (@($primaryLogs.Output).Count -gt 0) {
        Write-Tail $primaryLogs.Output 14
    }
    if (
        @($setupLogs.Output).Count -eq 0 -and
        @($primaryLogs.Output).Count -eq 0
    ) {
        $databaseLogs = Invoke-NativeCapture {
            docker compose --env-file $environmentFile --file $composeFile logs --no-color --tail 30 database
        }
        Write-Tail $databaseLogs.Output 14
    }
}

if ($failure) {
    $cleanup = Invoke-NativeCapture {
        docker compose --env-file $environmentFile --file $composeFile down --volumes --remove-orphans
    }
    if ($cleanup.ExitCode -eq 0) {
        Remove-Item -LiteralPath $environmentFile -ErrorAction SilentlyContinue
        if (
            (Test-Path -LiteralPath $runtimeDirectory) -and
            -not (Get-ChildItem -LiteralPath $runtimeDirectory -Force | Select-Object -First 1)
        ) {
            Remove-Item -LiteralPath $runtimeDirectory
        }
        Write-Host "Cleanup: PASS"
    }
    else {
        Write-Host "Cleanup: FAIL"
        Write-Tail $cleanup.Output 12
    }
    Write-Host "FAIL: $failure"
    exit 1
}
