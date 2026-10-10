param(
    [Parameter(Mandatory = $true)]
    [string]$RemoteRoot,
    [Parameter(Mandatory = $true)]
    [string]$StageDir
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath($RemoteRoot)
$stage = [System.IO.Path]::GetFullPath($StageDir)
$targetWatchdog = Join-Path $root 'watchdog'
$targetTests = Join-Path $targetWatchdog 'tests'
$backup = Join-Path $stage 'backup'
$launcher = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\NT8-Health-Watchdog.cmd'
$files = @(
    @{ staged = (Join-Path $stage 'recovery.py'); target = (Join-Path $targetWatchdog 'recovery.py') },
    @{ staged = (Join-Path $stage 'state_store.py'); target = (Join-Path $targetWatchdog 'state_store.py') },
    @{ staged = (Join-Path $stage 'test_recovery.py'); target = (Join-Path $targetTests 'test_recovery.py') }
)
$watchdogs = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^python(w)?\.exe$' -and
    $_.CommandLine -match 'watchdog\.monitor' -and
    $_.CommandLine -match [regex]::Escape((Split-Path $root -Leaf))
})
if ($watchdogs.Count -gt 1) {
    throw "Found $($watchdogs.Count) watchdog monitor processes for this checkout; refusing ambiguous stop."
}

$wasRunning = $watchdogs.Count -eq 1
$python = if ($wasRunning) { $watchdogs[0].ExecutablePath } else { (Get-Command python -ErrorAction Stop).Source }
$changed = $false
$stopped = $false

function Start-Watchdog {
    if (Test-Path -LiteralPath $launcher) {
        Start-Process -WindowStyle Hidden -FilePath $env:ComSpec -ArgumentList "/c `"$launcher`""
    } else {
        $cfg = Join-Path $root 'config.yaml'
        Start-Process -WindowStyle Hidden -FilePath $python -WorkingDirectory $root -ArgumentList "-m watchdog.monitor --config `"$cfg`""
    }

    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Seconds 1
        $found = Get-CimInstance Win32_Process | Where-Object {
            $_.Name -match '^python(w)?\.exe$' -and
            $_.CommandLine -match 'watchdog\.monitor' -and
            $_.CommandLine -match [regex]::Escape((Split-Path $root -Leaf))
        } | Select-Object -First 1
    } while (-not $found -and (Get-Date) -lt $deadline)
    if (-not $found) { throw 'Watchdog process did not return after deployment.' }
}

try {
    if (-not (Test-Path -LiteralPath $root)) { throw "Production checkout not found: $root" }
    if (-not (Test-Path -LiteralPath (Join-Path $root 'config.yaml'))) { throw 'Production config.yaml is missing.' }
    foreach ($file in $files) {
        if (-not (Test-Path -LiteralPath $file.staged)) { throw "Staged file missing: $($file.staged)" }
        if (-not (Test-Path -LiteralPath $file.target)) { throw "Production file missing: $($file.target)" }
    }

    New-Item -ItemType Directory -Force -Path $backup, $targetTests | Out-Null
    foreach ($file in $files) { Copy-Item -LiteralPath $file.target -Destination $backup -Force }

    if ($wasRunning) {
        Stop-Process -Id $watchdogs[0].ProcessId -Force
        $stopped = $true
        $deadline = (Get-Date).AddSeconds(20)
        do {
            Start-Sleep -Milliseconds 250
            $stillRunning = Get-Process -Id $watchdogs[0].ProcessId -ErrorAction SilentlyContinue
        } while ($stillRunning -and (Get-Date) -lt $deadline)
        if ($stillRunning) { throw 'Watchdog process did not stop.' }
    }

    $changed = $true
    foreach ($file in $files) { Copy-Item -LiteralPath $file.staged -Destination $file.target -Force }
    foreach ($file in $files) {
        $sourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.staged).Hash
        $targetHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.target).Hash
        if ($sourceHash -ne $targetHash) { throw "Hash check failed after copying $($file.target)" }
        Write-Output "DEPLOYED $($file.target) SHA256=$targetHash"
    }

    Push-Location $root
    try {
        & $python -m compileall watchdog
        if ($LASTEXITCODE -ne 0) { throw "Remote compileall failed with exit code $LASTEXITCODE" }
        & $python -m unittest watchdog.tests.test_recovery watchdog.tests.test_telegram_notifier -v
        if ($LASTEXITCODE -ne 0) { throw "Remote recovery tests failed with exit code $LASTEXITCODE" }
    } finally {
        Pop-Location
    }

    if ($wasRunning) { Start-Watchdog }
    Write-Output 'DEPLOYMENT_OK'
} catch {
    $failure = $_
    if ($changed) {
        foreach ($file in $files) {
            $saved = Join-Path $backup (Split-Path $file.target -Leaf)
            if (Test-Path -LiteralPath $saved) { Copy-Item -LiteralPath $saved -Destination $file.target -Force }
        }
        Write-Output 'ROLLBACK_RESTORED'
    }
    if ($wasRunning -and $stopped) {
        try { Start-Watchdog } catch { Write-Output "ROLLBACK_RESTART_FAILED: $_" }
    }
    throw $failure
}
