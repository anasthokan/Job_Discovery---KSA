# Restart Job Discovery on port 9002 so git pull is actually loaded.
# Health-ok is not enough: uvicorn keeps the old Python in memory until the process dies.
# Run in an elevated PowerShell on the server:
#   cd C:\Projects\Job_Discovery---KSA
#   powershell -ExecutionPolicy Bypass -File .\deploy\restart-api.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

function Get-Procs9002 {
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -and (
      ($_.Name -match 'python' -and $_.CommandLine -match 'uvicorn' -and $_.CommandLine -match '9002') -or
      ($_.CommandLine -match 'start-job-discovery\.bat')
    )
  }
}

function Stop-Api9002 {
  $pm2 = Get-Command pm2 -ErrorAction SilentlyContinue
  if ($pm2) {
    try {
      & pm2 restart job-discovery-ksa 2>$null
      if ($LASTEXITCODE -eq 0) {
        Write-Host "PM2 restarted job-discovery-ksa."
        return
      }
    } catch {
      Write-Host "PM2 restart skipped: $($_.Exception.Message)"
    }
  }

  $ids = @()
  foreach ($proc in @(Get-Procs9002)) {
    if ($proc.ProcessId) { $ids += [int]$proc.ProcessId }
  }
  try {
    $ids += @(
      Get-NetTCPConnection -LocalPort 9002 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    )
  } catch {}
  foreach ($procId in ($ids | Select-Object -Unique)) {
    if (-not $procId -or $procId -le 4) { continue }
    Write-Host "Stopping pid $procId on port 9002."
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
  }
  Start-Sleep -Seconds 2
}

Stop-Api9002

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
Write-Host "Port 9002 is running the current code."
