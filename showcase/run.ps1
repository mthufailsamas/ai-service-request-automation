$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot ".runtime"
$runtimeDirectory = Join-Path $runtimeRoot "showcase"
$resultFile = Join-Path $runtimeDirectory "e2e-result.json"
$outputDirectory = Join-Path $projectRoot "output"
$reportFile = Join-Path $outputDirectory "showcase-report.html"
$overrideFile = Join-Path $PSScriptRoot "compose.yaml"
$runner = Join-Path $projectRoot "checks\runners\end-to-end.ps1"
$renderer = Join-Path $PSScriptRoot "render_report.py"

function Remove-ShowcaseRuntime {
    Remove-Item -LiteralPath $resultFile -ErrorAction SilentlyContinue
    if (
        (Test-Path -LiteralPath $runtimeDirectory) -and
        -not (Get-ChildItem -LiteralPath $runtimeDirectory -Force | Select-Object -First 1)
    ) {
        Remove-Item -LiteralPath $runtimeDirectory
    }
    if (
        (Test-Path -LiteralPath $runtimeRoot) -and
        -not (Get-ChildItem -LiteralPath $runtimeRoot -Force | Select-Object -First 1)
    ) {
        Remove-Item -LiteralPath $runtimeRoot
    }
}

Write-Host "AI Service Request Automation - one-command showcase"
Write-Host "Scope: 7 fictional lifecycle cases; controlled fixture AI; IDR 0."
Write-Host "Result: 1 overwritten HTML report; disposable services cleaned automatically."
Write-Host ""

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "FAIL: Python was not found."
    exit 1
}

New-Item -ItemType Directory -Force -Path $runtimeDirectory | Out-Null
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
Remove-Item -LiteralPath $resultFile -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $reportFile -ErrorAction SilentlyContinue

$failure = $null
try {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $runner -ComposeOverride $overrideFile
    if ($LASTEXITCODE -ne 0) {
        throw "The controlled lifecycle did not complete."
    }
    if (-not (Test-Path -LiteralPath $resultFile)) {
        throw "The controlled lifecycle did not export its report evidence."
    }

    & python $renderer --input $resultFile --output $reportFile
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $reportFile)) {
        throw "The showcase HTML report could not be generated."
    }

    Write-Host ""
    Write-Host "Showcase report: READY"
    Write-Host "Report: $reportFile"
    Write-Host "Opening the report in the default browser..."
    try {
        Start-Process -FilePath $reportFile
    }
    catch {
        Write-Host "Automatic browser open: UNAVAILABLE; open the report path above."
    }
}
catch {
    $failure = $_.Exception.Message
}
finally {
    Remove-ShowcaseRuntime
}

if ($failure) {
    Write-Host "FAIL: $failure"
    exit 1
}

Write-Host "Temporary showcase evidence removed: PASS"
Write-Host "PASS: The one-command showcase completed."
