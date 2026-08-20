$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $projectRoot "compose.approved-action-check.yaml"

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
    Remove-Item Env:\AI_AUTOMATION_APPROVED_ACTION_PRIMARY_DB_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_APPROVED_ACTION_SANDBOX_DB_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_APPROVED_ACTION_SANDBOX_TOKEN -ErrorAction SilentlyContinue
}

Write-Host "AI Service Request Automation - approved downstream-action integration check"
Write-Host "Scope: 6 focused groups; fictional approved cases; local Service Desk; 0 AI calls."
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

$env:AI_AUTOMATION_APPROVED_ACTION_PRIMARY_DB_PASSWORD = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_APPROVED_ACTION_SANDBOX_DB_PASSWORD = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_APPROVED_ACTION_SANDBOX_TOKEN = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)

$failure = $null

try {
    $build = Invoke-NativeCapture {
        docker compose --file $composeFile build --quiet sandbox tests
    }
    if ($build.ExitCode -ne 0) {
        Write-Tail $build.Output 20
        throw "The approved-action images could not be prepared."
    }
    Write-Host "Application images: READY"

    $startup = Invoke-NativeCapture {
        docker compose --file $composeFile up `
            --wait `
            --wait-timeout 120 `
            primary-database `
            sandbox
    }
    if ($startup.ExitCode -ne 0) {
        Write-Tail $startup.Output 22
        throw "The disposable approved-action services did not become healthy."
    }
    Write-Host "Disposable services: HEALTHY"

    $tests = Invoke-NativeCapture {
        docker compose --file $composeFile run --rm --no-deps tests
    }
    if ($tests.ExitCode -ne 0) {
        Write-Tail $tests.Output 24
        throw "The approved-action integration groups failed."
    }
    if (-not ($tests.Output -match '^  Approved downstream-action gate: PASS$')) {
        throw "The approved-action completion marker was missing."
    }
    @($tests.Output | Where-Object {
        ([string]$_) -match '^(\[[1-6]/6\]|Approved downstream-action integration summary|  )'
    }) | ForEach-Object { Write-Host ([string]$_) }
    Write-Host ""
    Write-Host "PASS: Approved downstream actions passed their controlled local check."
}
catch {
    $failure = $_.Exception.Message
    Write-Host ""
    Write-Host "Relevant service diagnostics:"
    $logs = Invoke-NativeCapture {
        docker compose --file $composeFile logs --no-color --tail 40 sandbox
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
