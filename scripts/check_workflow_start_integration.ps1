$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $projectRoot "compose.workflow-start-check.yaml"

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
        ([string]$_) -match '^(FAIL:|\[[1-8]/8\]|Workflow-start integration summary|  )'
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

Write-Host "AI Service Request Automation - workflow-start integration check"
Write-Host "Scope: 8 local groups; fictional data; disposable storage; no AI call."
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

$env:AI_AUTOMATION_WORKFLOW_DB_PASSWORD = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_WORKFLOW_INTAKE_TOKEN = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_WORKFLOW_SESSION_SECRET = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_WORKFLOW_N8N_TOKEN = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_WORKFLOW_PRIMARY_TOKEN = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)
$env:AI_AUTOMATION_WORKFLOW_N8N_ENCRYPTION_KEY = (
    [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
)

$failureMessage = $null

try {
    $build = Invoke-NativeCapture {
        docker compose --file $composeFile build --quiet primary-api
    }
    if ($build.ExitCode -ne 0) {
        Write-BoundedLines -Lines $build.Output -Tail 20
        throw "The workflow-start test image could not be prepared. Return the terminal output to Codex."
    }
    Write-Host "Application image: READY"

    $startup = Invoke-NativeCapture {
        docker compose --file $composeFile up `
            --wait `
            --wait-timeout 120 `
            primary-api n8n
    }
    if ($startup.ExitCode -ne 0) {
        Write-BoundedLines -Lines $startup.Output -Tail 25
        throw "The workflow-start services did not become healthy. Return the terminal output to Codex."
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
        $serviceLogResult = Invoke-NativeCapture {
            docker compose --file $composeFile logs `
                --no-color `
                --tail 60 `
                n8n primary-api
        }
        $relevantLogs = @($serviceLogResult.Output | Where-Object {
            $line = [string]$_
            $line -match '(?i)(rangeerror|traceback|exception|error|failed|POST /)' `
                -and $line -notmatch 'Failed to start Python task runner'
        })
        if ($relevantLogs.Count -gt 0) {
            Write-BoundedLines -Lines $relevantLogs -Tail 20
        }
        else {
            Write-BoundedLines -Lines $serviceLogResult.Output -Tail 12
        }
        throw "The workflow-start integration check failed. Return the terminal output to Codex."
    }

    Write-Host ""
    Write-Host "PASS: Workflow start passed its controlled local runtime check."
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
            $failureMessage = "Disposable Docker cleanup failed. Return the terminal output to Codex."
        }
    }
    Remove-Item Env:\AI_AUTOMATION_WORKFLOW_DB_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_WORKFLOW_INTAKE_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_WORKFLOW_SESSION_SECRET -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_WORKFLOW_N8N_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_WORKFLOW_PRIMARY_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:\AI_AUTOMATION_WORKFLOW_N8N_ENCRYPTION_KEY -ErrorAction SilentlyContinue
}

if ($failureMessage) {
    Write-Host "FAIL: $failureMessage"
    exit 1
}
