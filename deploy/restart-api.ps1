# Restart Job Discovery on the IIS server so port 9001 answers again.
# Run in an elevated PowerShell ON 74.208.184.75:
#   cd C:\Projects\Job_Discovery
#   powershell -ExecutionPolicy Bypass -File .\deploy\restart-api.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$pm2 = Get-Command pm2 -ErrorAction SilentlyContinue
if ($pm2) {
  pm2 restart job-discovery --update-env
  if ($LASTEXITCODE -ne 0) {
    pm2 start (Join-Path $root "ecosystem.config.cjs")
  }
  pm2 save
} else {
  $listening = Get-NetTCPConnection -LocalPort 9001 -State Listen -ErrorAction SilentlyContinue
  if ($listening) {
    Write-Host "API is already listening on port 9001. Left it running."
  } else {
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "start-job-discovery.bat" -WorkingDirectory $root -WindowStyle Minimized
  }
}

try {
  $watch = Join-Path $PSScriptRoot "watch-api.ps1"
  $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$watch`""
  $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 2) -RepetitionDuration ([TimeSpan]::MaxValue)
  Register-ScheduledTask -TaskName "JobDiscoveryHealth" -Action $action -Trigger $trigger -Force | Out-Null
  Write-Host "Health watchdog scheduled every 2 minutes."
} catch {
  Write-Host "Watchdog task was not created: $($_.Exception.Message)"
}

Start-Sleep -Seconds 3
$health = Invoke-RestMethod -Uri "http://127.0.0.1:9001/api/health" -TimeoutSec 15
Write-Host ($health | ConvertTo-Json -Compress)
