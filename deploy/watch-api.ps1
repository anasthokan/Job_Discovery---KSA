# If port 9002 stops answering, restart it. PM2 leaves a hung process marked online.
$root = Split-Path $PSScriptRoot -Parent
$log = Join-Path $root "data\api-watch.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

function Write-Watch($message) {
  $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
  Add-Content -Path $log -Value $line
}

try {
  $response = Invoke-WebRequest -Uri "http://127.0.0.1:9002/api/health" -TimeoutSec 8 -UseBasicParsing
  if ($response.StatusCode -eq 200) {
    exit 0
  }
  Write-Watch "health status $($response.StatusCode)"
} catch {
  Write-Watch "health failed: $($_.Exception.Message)"
}

Write-Watch "left the process running"
