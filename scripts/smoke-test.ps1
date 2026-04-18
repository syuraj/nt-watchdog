param(
    [string]$ProjectRoot = "",
    [int]$MockPort = 18999,
    [string]$Mode = "ok",
    [int]$MaxCycles = 3,
    [switch]$Cleanup = $false
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

$args = @($cli, "smoke-test", "--mock-port", "$MockPort", "--mode", $Mode, "--max-cycles", "$MaxCycles")
if ($Cleanup) {
    $args += "--cleanup"
}

Set-Location $ProjectRoot
& $pythonExe @args
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

