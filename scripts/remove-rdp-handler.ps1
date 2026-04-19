# Unregisters the RDP disconnect-handler Scheduled Task.
# Must be run elevated.

param(
    [string]$TaskName = "NT8-RDP-Redirect"
)

$ErrorActionPreference = "Stop"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "removed task '$TaskName'"
} else {
    Write-Host "task '$TaskName' not found (already removed?)"
}
