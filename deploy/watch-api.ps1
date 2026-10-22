# If port 9002 stops answering, start it again. Never touches the API on port 9001.
$ErrorActionPreference = "Continue"
$root = Split-Path $PSScriptRoot -Parent
$log = Join-Path $root "data\api-watch.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

function Write-Watch($message) {
  $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
  Add-Content -Path $log -Value $line
  Write-Host $line
}

function Test-ApiHealthy {
  try {
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:9002/api/health" -TimeoutSec 8 -UseBasicParsing
    return ($response.StatusCode -eq 200)
  } catch {
    Write-Watch "health failed: $($_.Exception.Message)"
    return $false
  }
}

function Get-Procs9002 {
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -and (
      ($_.Name -match 'python' -and $_.CommandLine -match 'uvicorn' -and $_.CommandLine -match '9002') -or
      ($_.CommandLine -match 'start-job-discovery\.bat')
    )
  }
}

function Stop-Hung9002 {
  $listeners = @()
  try {
    $listeners = @(
      Get-NetTCPConnection -LocalPort 9002 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    )
  } catch {
    $listeners = @()
  }
  foreach ($procId in $listeners) {
    if (-not $procId -or $procId -le 4) { continue }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $procId" -ErrorAction SilentlyContinue
    $cmd = ""
    if ($proc) { $cmd = [string]$proc.CommandLine }
    if ($cmd -notmatch '9002') {
      Write-Watch "port 9002 pid $procId is not the KSA app; left it alone"
      continue
    }
    Write-Watch "stopping hung pid $procId"
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
  }
}

if (Test-ApiHealthy) {
  exit 0
}

$existing = @(Get-Procs9002)
$booting = $false
foreach ($proc in $existing) {
  if ($proc.Name -notmatch 'python') { continue }
  try {
    $created = [Management.ManagementDateTimeConverter]::ToDateTime($proc.CreationDate)
    if (((Get-Date) - $created).TotalSeconds -lt 40) {
      $booting = $true
    }
  } catch {
  }
}
if ($booting) {
  Write-Watch "uvicorn on 9002 is still starting"
  exit 0
}

if ($existing) {
  Stop-Hung9002
  Start-Sleep -Seconds 2
}

$batRunning = @(Get-Procs9002 | Where-Object { $_.CommandLine -match 'start-job-discovery\.bat' })
if (-not $batRunning) {
  $bat = Join-Path $root "start-job-discovery.bat"
  Write-Watch "starting $bat"
  Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$bat`"" -WorkingDirectory $root -WindowStyle Minimized
} else {
  Write-Watch "start-job-discovery.bat is already running"
}

$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Seconds 3
  try {
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:9002/api/health" -TimeoutSec 5 -UseBasicParsing
    if ($response.StatusCode -eq 200) {
      Write-Watch "port 9002 is answering"
      exit 0
    }
  } catch {
  }
}

Write-Watch "port 9002 still not answering"
exit 1
