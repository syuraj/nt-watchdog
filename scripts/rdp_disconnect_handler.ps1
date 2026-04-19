# Prevents WPF / NinjaTrader chart rendering from freezing when an RDP session
# disconnects. On disconnect, Windows detaches the virtual display driver from
# the GPU, which suspends Direct3D Present() calls and stalls the WPF UI thread
# — even though market data keeps flowing. Redirecting the disconnected session
# to the console (Session 1) re-binds a display and rendering resumes.
#
# Intended to be invoked by a Scheduled Task triggered on Event 24 in
# Microsoft-Windows-TerminalServices-LocalSessionManager/Operational.
#
# Idempotent: exits 0 when no disconnected session is found.
#
# Optional parameter: -TargetUser <name> limits the redirect to sessions owned
# by that user. When omitted, the first Disc session found is redirected.

param(
    [string]$TargetUser = ""
)

$ErrorActionPreference = "Continue"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$logPath = Join-Path $repoRoot "watchdog\logs\rdp_handler.log"
$logDir = Split-Path $logPath -Parent
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

function Write-Log {
    param([string]$msg)
    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    Add-Content -Path $logPath -Value "$ts $msg"
}

Write-Log "handler invoked TargetUser='$TargetUser'"

# Event 24 fires the moment Windows flags the session as disconnecting, but
# qwinsta may still show it as Active for a second or two before settling to
# Disc. Poll up to ~15s so the handler doesn't miss the window.
$maxAttempts = 15
$attempt = 0
while ($attempt -lt $maxAttempts) {
    $attempt++
    $raw = (qwinsta 2>&1)
    if ($LASTEXITCODE -ne 0) {
        Write-Log "qwinsta failed exit=$LASTEXITCODE out='$raw'"
        Start-Sleep -Seconds 1
        continue
    }
    # Diagnostic: log full table on first attempt so we can see actual states.
    if ($attempt -eq 1) {
        foreach ($dl in ($raw -split "`r?`n")) {
            if (-not [string]::IsNullOrWhiteSpace($dl)) { Write-Log "qwinsta: $dl" }
        }
    }

    # qwinsta columns: SESSIONNAME USERNAME ID STATE TYPE DEVICE
    # Lines may start with ">" for the current session. Skip header.
    $lines = ($raw -split "`r?`n") | Select-Object -Skip 1
    foreach ($line in $lines) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        # qwinsta output is fixed-width but SESSIONNAME / USERNAME can be blank.
        # Tokenize and locate the first numeric (ID); STATE is the token after it.
        # USERNAME is the token immediately before ID when present.
        $stripped = $line -replace '^\s*>', ' '
        $tokens = ($stripped -split '\s+') | Where-Object { $_ -ne "" }
        if ($tokens.Count -lt 3) { continue }
        $idIdx = -1
        for ($i = 0; $i -lt $tokens.Count; $i++) {
            if ($tokens[$i] -match '^\d+$') { $idIdx = $i; break }
        }
        if ($idIdx -lt 0 -or $idIdx + 1 -ge $tokens.Count) { continue }
        $id = $tokens[$idIdx]
        $state = $tokens[$idIdx + 1]
        # USERNAME is the token before ID IF it's not a known session name.
        $user = ""
        if ($idIdx -ge 1) {
            $prev = $tokens[$idIdx - 1]
            if ($prev -notin @("services", "console", "rdp-tcp")) { $user = $prev }
        }
        if ($state -ne "Disc") { continue }
        if ($TargetUser -and $user -ne $TargetUser) { continue }

        Write-Log "redirecting session id=$id user='$user' attempt=$attempt"
        & tscon $id /dest:console 2>&1 | ForEach-Object { Write-Log "tscon: $_" }
        $rc = $LASTEXITCODE
        Write-Log "tscon exit=$rc"
        exit $rc
    }

    Start-Sleep -Seconds 1
}

Write-Log "no disconnected session found after $maxAttempts attempts"
exit 0
