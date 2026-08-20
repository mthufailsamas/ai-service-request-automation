$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $projectRoot "compose.human-resume-check.yaml"

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
    @($Lines) | Select-Object -Last $Count | ForEach-Object {
        Write-Host ([string]$_)
    }
}

function Remove-TemporaryEnvironment {
    Remove-Item Env:\AI_AUTOMATION_HUMAN_RESUME_DB_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_HUMAN_RESUME_INTAKE_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_HUMAN_RESUME_SESSION_SECRET -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_HUMAN_RESUME_PRIMARY_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_HUMAN_RESUME_N8N_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_HUMAN_RESUME_N8N_ENCRYPTION_KEY -ErrorAction SilentlyContinue
}

Write-Host "AI Service Request Automation - human-decision resume integration check"
Write-Host "Scope: 6 focused groups; fictional data; local n8n; 0 AI calls."
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

$env:AI_AUTOMATION_HUMAN_RESUME_DB_PASSWORD = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_HUMAN_RESUME_INTAKE_TOKEN = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_HUMAN_RESUME_SESSION_SECRET = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_HUMAN_RESUME_PRIMARY_TOKEN = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_HUMAN_RESUME_N8N_TOKEN = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_HUMAN_RESUME_N8N_ENCRYPTION_KEY = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)

$failure = $null

try {
    $build = Invoke-NativeCapture {
        docker compose --file $composeFile build --quiet primary-api
    }
    if ($build.ExitCode -ne 0) {
        Write-Tail $build.Output
        throw "The human-resume application image could not be prepared."
    }
    Write-Host "Application image: READY"

    $startup = Invoke-NativeCapture {
        docker compose --file $composeFile up --wait --wait-timeout 120 primary-api n8n
    }
    if ($startup.ExitCode -ne 0) {
        Write-Tail $startup.Output 22
        throw "The disposable human-resume services did not become healthy."
    }
    Write-Host "Disposable services: HEALTHY"

    $tests = Invoke-NativeCapture {
        docker compose --file $composeFile run --rm --no-deps tests
    }
    if ($tests.ExitCode -ne 0) {
        Write-Tail $tests.Output 24
        throw "The human-resume integration groups failed."
    }
    if (-not ($tests.Output -match '^  Human-decision resume gate: PASS$')) {
        throw "The human-resume completion marker was missing."
    }
    @($tests.Output | Where-Object {
        ([string]$_) -match '^(\[[1-6]/6\]|Human-decision resume integration summary|  )'
    }) | ForEach-Object { Write-Host ([string]$_) }
    Write-Host ""
    Write-Host "PASS: Human-decision resume handoff passed its controlled local check."
}
catch {
    $failure = $_.Exception.Message
    Write-Host ""
    Write-Host "Relevant service diagnostics:"
    $logs = Invoke-NativeCapture {
        docker compose --file $composeFile logs --no-color --tail 40 primary-api n8n
    }
    $relevant = @($logs.Output | Where-Object {
        ([string]$_) -match '(?i)(traceback|exception|error|failed|POST /)'
    })
    Write-Tail $(if ($relevant.Count) { $relevant } else { $logs.Output }) 16
}
finally {
    $cleanup = Invoke-NativeCapture {
        docker compose --file $composeFile down --volumes --remove-orphans
    }
    if ($cleanup.ExitCode -eq 0) {
        Write-Host "Cleanup: PASS"
    }
    else {
        Write-Host "Cleanup: FAIL"
        Write-Tail $cleanup.Output 12
        if (-not $failure) {
            $failure = "Disposable Docker cleanup failed."
        }
    }
    Remove-TemporaryEnvironment
}

if ($failure) {
    Write-Host "FAIL: $failure"
    exit 1
}
