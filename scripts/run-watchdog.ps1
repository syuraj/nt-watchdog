param(
    [string]$ProjectRoot = "",
    [string]$ConfigPath = "",
    [int]$MaxCycles = 0
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$pythonExe = if (Test-Path $venvPython) { $venvPython } else { "python" }
$cli = Join-Path $ProjectRoot "scripts\manage_watchdog.py"

if (-not (Test-Path $cli)) {
    throw "Python CLI not found at $cli"
}

$args = @($cli, "run")
if (-not [string]::IsNullOrWhiteSpace($ConfigPath)) {
    $args += @("--config", $ConfigPath)
}
if ($MaxCycles -gt 0) {
    $args += @("--max-cycles", "$MaxCycles")
}

Set-Location $ProjectRoot
& $pythonExe @args
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

