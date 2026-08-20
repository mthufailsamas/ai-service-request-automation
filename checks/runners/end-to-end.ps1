param(
    [string]$ComposeOverride = ""
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$composeFile = Join-Path $projectRoot "checks\compose\end-to-end.yaml"
$composeArguments = @(
    "--project-directory", $projectRoot,
    "--file", $composeFile
)
if ($ComposeOverride) {
    $resolvedOverride = (Resolve-Path -LiteralPath $ComposeOverride).Path
    $composeArguments += @("--file", $resolvedOverride)
}

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
    @(
        "AI_AUTOMATION_E2E_PRIMARY_DB_PASSWORD",
        "AI_AUTOMATION_E2E_SANDBOX_DB_PASSWORD",
        "AI_AUTOMATION_E2E_INTAKE_TOKEN",
        "AI_AUTOMATION_E2E_SESSION_SECRET",
        "AI_AUTOMATION_E2E_PRIMARY_TOKEN",
        "AI_AUTOMATION_E2E_N8N_TOKEN",
        "AI_AUTOMATION_E2E_N8N_ENCRYPTION_KEY",
        "AI_AUTOMATION_E2E_SANDBOX_TOKEN"
    ) | ForEach-Object {
        Remove-Item "Env:\$_" -ErrorAction SilentlyContinue
    }
}

Write-Host "AI Service Request Automation - full end-to-end integration check"
Write-Host "Scope: 7 focused groups; 7 fictional cases; local services; fixture AI."
Write-Host "Hosted or paid AI calls: 0"
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

$env:AI_AUTOMATION_E2E_PRIMARY_DB_PASSWORD = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$env:AI_AUTOMATION_E2E_SANDBOX_DB_PASSWORD = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$env:AI_AUTOMATION_E2E_INTAKE_TOKEN = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$env:AI_AUTOMATION_E2E_SESSION_SECRET = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$env:AI_AUTOMATION_E2E_PRIMARY_TOKEN = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$env:AI_AUTOMATION_E2E_N8N_TOKEN = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$env:AI_AUTOMATION_E2E_N8N_ENCRYPTION_KEY = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$env:AI_AUTOMATION_E2E_SANDBOX_TOKEN = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$failure = $null

try {
    $resolvedConfig = Invoke-NativeCapture {
        docker compose @composeArguments config --format json
    }
    if ($resolvedConfig.ExitCode -ne 0) {
        Write-Tail $resolvedConfig.Output
        throw "The end-to-end Compose contract is invalid."
    }
    $configObject = (@($resolvedConfig.Output) -join "`n") | ConvertFrom-Json
    $invalidTmpfs = @(
        $configObject.services.psobject.Properties.Value.tmpfs |
            Where-Object { $_ -and -not ([string]$_).StartsWith("/") }
    )
    if ($invalidTmpfs.Count -gt 0) {
        throw "The end-to-end Compose contract contains an invalid tmpfs path."
    }

    $build = Invoke-NativeCapture {
        docker compose @composeArguments build --quiet primary-api sandbox-api
    }
    if ($build.ExitCode -ne 0) {
        Write-Tail $build.Output
        throw "The end-to-end application images could not be prepared."
    }
    Write-Host "Application images: READY"

    $startup = Invoke-NativeCapture {
        docker compose @composeArguments up --wait --wait-timeout 150 primary-api sandbox-api n8n
    }
    if ($startup.ExitCode -ne 0) {
        Write-Tail $startup.Output 22
        throw "The disposable end-to-end services did not become healthy."
    }
    Write-Host "Disposable services: HEALTHY"

    $tests = Invoke-NativeCapture {
        docker compose @composeArguments run --rm --no-deps tests
    }
    if ($tests.ExitCode -ne 0) {
        Write-Tail $tests.Output 26
        throw "The full end-to-end integration groups failed."
    }
    if (-not ($tests.Output -match '^  Full end-to-end gate: PASS$')) {
        throw "The full end-to-end completion marker was missing."
    }
    @($tests.Output | Where-Object {
        ([string]$_) -match '^(\[[1-7]/7\]|Full end-to-end integration summary|  )'
    }) | ForEach-Object { Write-Host ([string]$_) }
    Write-Host ""
    Write-Host "PASS: The accepted local lifecycle passed its combined end-to-end check."
}
catch {
    $failure = $_.Exception.Message
    Write-Host ""
    Write-Host "Relevant service diagnostics:"
    $logs = Invoke-NativeCapture {
        docker compose @composeArguments logs --no-color --tail 60 primary-api sandbox-api n8n-setup n8n
    }
    $relevant = @($logs.Output | Where-Object {
        (([string]$_) -match '(?i)(traceback|exception|error|failed|POST /)') -and
        (([string]$_) -notmatch 'Failed to start Python task runner in internal mode')
    })
    Write-Tail $(if ($relevant.Count) { $relevant } else { $logs.Output }) 18
}
finally {
    $cleanup = Invoke-NativeCapture {
        docker compose @composeArguments down --volumes --remove-orphans
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
