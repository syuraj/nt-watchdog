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
$logPath = Join-Path $env:ProgramData "nt8-health\rdp_handler.log"
$logDir = Split-Path $logPath -Parent
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

function Write-Log {
    param([string]$msg)
    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    Add-Content -Path $logPath -Value "$ts $msg"
}

Write-Log "handler invoked TargetUser='$TargetUser'"

$raw = (qwinsta 2>&1)
if ($LASTEXITCODE -ne 0) {
    Write-Log "qwinsta failed exit=$LASTEXITCODE out='$raw'"
    exit 1
}

# qwinsta columns: SESSIONNAME USERNAME ID STATE TYPE DEVICE
# Lines may start with ">" for the current session. Skip header.
$lines = ($raw -split "`r?`n") | Select-Object -Skip 1
foreach ($line in $lines) {
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    # Flexible match: optional leading '>', session name, optional user, ID, STATE
    if ($line -match '^\s*>?\s*(\S+)\s+(\S+)?\s+(\d+)\s+(\S+)') {
        $sessionName = $Matches[1]
        $user = $Matches[2]
        $id = $Matches[3]
        $state = $Matches[4]
        # When the username is absent, the regex captures the ID into group 2 instead.
        if ($user -match '^\d+$') {
            $user = ""
            $id = $Matches[2]
            $state = $Matches[3]
        }
        if ($state -ne "Disc") { continue }
        if ($TargetUser -and $user -ne $TargetUser) { continue }

        Write-Log "redirecting session id=$id user='$user' name='$sessionName' to console"
        & tscon $id /dest:console 2>&1 | ForEach-Object { Write-Log "tscon: $_" }
        $rc = $LASTEXITCODE
        Write-Log "tscon exit=$rc"
        exit $rc
    }
}

Write-Log "no disconnected session found"
exit 0
