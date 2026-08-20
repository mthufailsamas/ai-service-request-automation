$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $PSScriptRoot "compose.yaml"
$runtimeDirectory = Join-Path $projectRoot ".runtime"
$environmentFile = Join-Path $runtimeDirectory "guided-demo.env"
$modelUseMarker = Join-Path $runtimeDirectory "guided-demo-ollama-active"

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

Write-Host "AI Service Request Automation - guided demo cleanup"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "FAIL: Docker was not found, so disposable state cannot be verified."
    exit 1
}

if (-not (Test-Path -LiteralPath $environmentFile)) {
    New-Item -ItemType Directory -Force -Path $runtimeDirectory | Out-Null
    @(
        "AI_AUTOMATION_DEMO_DB_PASSWORD=cleanup-placeholder-00000000000000000000000000000000"
        "AI_AUTOMATION_DEMO_INTAKE_TOKEN=cleanup-placeholder-00000000000000000000000000000000"
        "AI_AUTOMATION_DEMO_SESSION_SECRET=cleanup-placeholder-00000000000000000000000000000000"
        "AI_AUTOMATION_DEMO_WORKFLOW_TOKEN=cleanup-placeholder-00000000000000000000000000000000"
    ) | Set-Content -LiteralPath $environmentFile -Encoding ascii
}

$cleanup = Invoke-NativeCapture {
    docker compose --env-file $environmentFile --file $composeFile down --volumes --remove-orphans
}
if ($cleanup.ExitCode -ne 0) {
    @($cleanup.Output) | Select-Object -Last 14 | ForEach-Object {
        Write-Host ([string]$_)
    }
    Write-Host "Cleanup: FAIL"
    exit 1
}

$modelCleanupRequired = Test-Path -LiteralPath $modelUseMarker
if ($modelCleanupRequired) {
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        Write-Host "Cleanup: FAIL"
        Write-Host "Ollama was used by this demo but is no longer available for unload verification."
        exit 1
    }
    $stopResult = Invoke-NativeCapture { ollama stop qwen3:4b-instruct }
    $runningModels = Invoke-NativeCapture { ollama ps }
    if (
        $runningModels.ExitCode -ne 0 -or
        ($runningModels.Output -join "`n") -match "qwen3:4b-instruct"
    ) {
        Write-Host "Cleanup: FAIL"
        Write-Host "The accepted local model remained loaded."
        exit 1
    }
    Remove-Item -LiteralPath $modelUseMarker -ErrorAction SilentlyContinue
}

Remove-Item -LiteralPath $environmentFile -ErrorAction SilentlyContinue
if (
    (Test-Path -LiteralPath $runtimeDirectory) -and
    -not (Get-ChildItem -LiteralPath $runtimeDirectory -Force | Select-Object -First 1)
) {
    Remove-Item -LiteralPath $runtimeDirectory
}

Write-Host "Disposable Docker state removed: PASS"
Write-Host "Accepted local model cleanup: $(if ($modelCleanupRequired) { 'PASS' } else { 'NOT REQUIRED' })"
Write-Host "Cleanup: PASS"
