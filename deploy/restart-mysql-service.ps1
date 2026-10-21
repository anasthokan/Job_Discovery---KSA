# Frees stuck MySQL sessions by restarting only the database service.
# job-discovery and ai-marketing-backend are left running.
# Run in Administrator PowerShell:
#   cd C:\Projects\Job_Discovery
#   powershell -ExecutionPolicy Bypass -File .\deploy\restart-mysql-service.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$services = @(Get-Service | Where-Object {
  $_.Name -match 'mysql|maria' -or $_.DisplayName -match 'MySQL|MariaDB'
})
if (-not $services) {
  throw "No MySQL or MariaDB Windows service was found."
}

foreach ($svc in $services) {
  Write-Host "Restarting only $($svc.Name) ($($svc.DisplayName))"
  Restart-Service -Name $svc.Name -Force
}

Start-Sleep -Seconds 8
python (Join-Path $PSScriptRoot "open_mysql_connections.py")
curl.exe -m 30 "http://127.0.0.1:9002/api/jobs?state=All&platform=All&days=30&limit=5&page=1"
Write-Host ""
