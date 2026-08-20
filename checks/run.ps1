param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("recovery-operations", "end-to-end", "locked-evaluation")]
    [string]$CheckName
)

$ErrorActionPreference = "Stop"
$runner = Join-Path $PSScriptRoot "runners\$CheckName.ps1"
& $runner
exit $LASTEXITCODE
