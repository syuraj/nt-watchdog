param(
    [string]$ProjectRoot = "",
    [string]$PythonExe = "python",
    [string]$BridgeUrl = "http://localhost:8899",
    [string]$NtExecutablePath = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$pythonExeResolved = if (Test-Path $venvPython) { $venvPython } else { $PythonExe }
$cli = Join-Path $ProjectRoot "scripts\manage_watchdog.py"

if (-not (Test-Path $cli)) {
    throw "Python CLI not found at $cli"
}

$args = @($cli, "setup", "--python", $PythonExe, "--bridge-url", $BridgeUrl)
if (-not [string]::IsNullOrWhiteSpace($NtExecutablePath)) {
    $args += @("--nt-executable-path", $NtExecutablePath)
}

Set-Location $ProjectRoot
& $pythonExeResolved @args
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

