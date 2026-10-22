# Restart Job Discovery on the IIS server so port 9002 answers again.
# Run in an elevated PowerShell on the server:
#   cd C:\Projects\Job_Discovery---KSA
#   powershell -ExecutionPolicy Bypass -File .\deploy\restart-api.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$watch = Join-Path $PSScriptRoot "watch-api.ps1"
try {
  $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$watch`""
  # Task Scheduler rejects TimeSpan.MaxValue (P99999999D). 9999 days is the longest duration it accepts.
  $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 2) -RepetitionDuration (New-TimeSpan -Days 9999)
  Register-ScheduledTask -TaskName "JobDiscoveryHealth" -Action $action -Trigger $trigger -Force | Out-Null
  Write-Host "Health watchdog scheduled every 2 minutes."
} catch {
  Write-Host "Watchdog task was not created: $($_.Exception.Message)"
}

& $watch
if ($LASTEXITCODE -ne 0) {
  Write-Host "Port 9002 did not answer yet. Check data\api-watch.log"
  exit $LASTEXITCODE
}
