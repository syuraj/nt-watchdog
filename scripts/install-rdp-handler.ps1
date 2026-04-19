# Registers a Scheduled Task that redirects disconnected RDP sessions to the
# console, preventing NT8 chart freeze on RDP disconnect.
#
# Trigger: Event ID 24 in Microsoft-Windows-TerminalServices-LocalSessionManager/Operational
# (this log is enabled by default — no audit policy change required).
# Action: run rdp_disconnect_handler.ps1 as SYSTEM (needs SYSTEM to call tscon
# across sessions).
#
# Must be run elevated (administrator).

param(
    [string]$TargetUser = $env:USERNAME,
    [string]$TaskName = "NT8-RDP-Redirect"
)

$ErrorActionPreference = "Stop"

# Locate handler script next to this installer.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$handlerPath = Join-Path $scriptDir "rdp_disconnect_handler.ps1"
if (-not (Test-Path $handlerPath)) {
    Write-Error "handler script not found at $handlerPath"
    exit 1
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$handlerPath`" -TargetUser `"$TargetUser`""

# Event subscription XML for Event ID 24 in the TS LocalSessionManager log.
$eventXml = @"
<QueryList>
  <Query Id="0" Path="Microsoft-Windows-TerminalServices-LocalSessionManager/Operational">
    <Select Path="Microsoft-Windows-TerminalServices-LocalSessionManager/Operational">*[System[EventID=24]]</Select>
  </Query>
</QueryList>
"@

$cimClass = Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler
$trigger = New-CimInstance -CimClass $cimClass -ClientOnly
$trigger.Enabled = $true
$trigger.Subscription = $eventXml

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

# Unregister if exists so we can re-register cleanly.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "Redirects disconnected RDP sessions to console so NT8 chart rendering doesn't stall."

Write-Host "installed task '$TaskName' for user '$TargetUser'"
Write-Host "handler: $handlerPath"
Write-Host "log: $env:ProgramData\nt8-health\rdp_handler.log"
