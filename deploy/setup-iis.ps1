# Creates/updates IIS site for Job Discovery (reverse proxy to uvicorn :9001).
# Run ON the Windows IIS server as Administrator:
#   cd C:\Projects\Job_Discovery
#   powershell -ExecutionPolicy Bypass -File .\deploy\setup-iis.ps1

param(
  [string]$SiteName = "agentsksa",
  [string]$AppRoot = "C:\Projects\Job_Discovery - KSA",
  [string]$ServerIP = "74.208.184.75",
  [string]$HostName = "agentsksa.atozeesolutions.com",
  [int]$Port = 80,
  [int]$AppPort = 9001
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path (Join-Path $AppRoot "api.py"))) {
  throw "Missing $AppRoot\api.py - copy the Job_Discovery folder to the IIS server first."
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
  throw "python is not on PATH. Install Python 3.11+ and tick 'Add python.exe to PATH'."
}

Write-Host "==> pip install..." -ForegroundColor Cyan
Push-Location $AppRoot
python -m pip install -r requirements.txt
Pop-Location

$envFile = Join-Path $AppRoot ".env"
$envExample = Join-Path $AppRoot ".env.example"
if (-not (Test-Path $envFile)) {
  if (Test-Path $envExample) {
    Copy-Item $envExample $envFile
    Write-Host "Created $envFile from .env.example." -ForegroundColor Yellow
    Write-Host "EDIT IT NOW: set MYSQL_HOST / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE then continue." -ForegroundColor Yellow
  } else {
    Write-Host "Missing .env - MySQL save + scheduled scrape will stay off until you add one." -ForegroundColor Yellow
  }
} else {
  Write-Host "Using existing .env for MySQL + scrape schedule." -ForegroundColor Green
}

Write-Host "==> Firewall TCP $Port..." -ForegroundColor Cyan
$fwName = "Job Discovery IIS $Port"
if (-not (Get-NetFirewallRule -DisplayName $fwName -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName $fwName -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
}
$fwHttps = "Job Discovery IIS 443"
if (-not (Get-NetFirewallRule -DisplayName $fwHttps -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName $fwHttps -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow | Out-Null
}
$fwHttps = "Job Discovery IIS 443"
if (-not (Get-NetFirewallRule -DisplayName $fwHttps -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName $fwHttps -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow | Out-Null
}

Write-Host "==> IIS site..." -ForegroundColor Cyan
Import-Module WebAdministration

$webConfigPath = Join-Path $AppRoot "web.config"
if (-not (Test-Path $webConfigPath)) {
  throw "Missing $webConfigPath"
}

$webConfig = @"
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <system.webServer>
    <proxy enabled="true" timeout="00:12:00" responseBufferLimit="0" />
    <rewrite>
      <rules>
        <rule name="ReverseProxyToJobDiscovery" stopProcessing="true">
          <match url="(.*)" />
          <action type="Rewrite" url="http://127.0.0.1:$AppPort/{R:1}" appendQueryString="true" />
        </rule>
      </rules>
    </rewrite>
    <httpErrors existingResponse="PassThrough" />
  </system.webServer>
</configuration>
"@
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($webConfigPath, $webConfig, $utf8)

if (-not (Test-Path "IIS:\AppPools\$SiteName")) {
  New-WebAppPool -Name $SiteName | Out-Null
}
Set-ItemProperty "IIS:\AppPools\$SiteName" -Name managedRuntimeVersion -Value ""

$existing = Get-Website -Name $SiteName -ErrorAction SilentlyContinue
if (-not $existing) {
  New-Website -Name $SiteName -PhysicalPath $AppRoot -Port $Port -IPAddress $ServerIP -ApplicationPool $SiteName | Out-Null
} else {
  Set-ItemProperty "IIS:\Sites\$SiteName" -Name physicalPath -Value $AppRoot
  $site = Get-Website -Name $SiteName
  foreach ($b in @($site.bindings.Collection)) {
    Remove-WebBinding -Name $SiteName -BindingInformation $b.bindingInformation -Protocol $b.protocol -ErrorAction SilentlyContinue
  }
  New-WebBinding -Name $SiteName -Protocol http -Port $Port -HostHeader $HostName
  New-WebBinding -Name $SiteName -Protocol http -IPAddress $ServerIP -Port $Port
}
if (-not (Get-WebBinding -Name $SiteName -Protocol http -ErrorAction SilentlyContinue | Where-Object { $_.bindingInformation -like "*:${Port}:$HostName" })) {
  New-WebBinding -Name $SiteName -Protocol http -Port $Port -HostHeader $HostName
}

$cert = Get-ChildItem Cert:\LocalMachine\My | Where-Object {
  $_.DnsNameList -match [regex]::Escape($HostName) -or $_.Subject -match [regex]::Escape($HostName)
} | Select-Object -First 1
if ($cert) {
  if (-not (Get-WebBinding -Name $SiteName -Protocol https -ErrorAction SilentlyContinue | Where-Object { $_.bindingInformation -like "*:443:$HostName" })) {
    New-WebBinding -Name $SiteName -Protocol https -Port 443 -HostHeader $HostName -SslFlags 1
  }
  $httpsBinding = Get-WebBinding -Name $SiteName -Protocol https | Where-Object { $_.bindingInformation -like "*:443:$HostName" }
  if ($httpsBinding) {
    $httpsBinding.AddSslCertificate($cert.Thumbprint, "My")
  }
  Write-Host "HTTPS bound with certificate $($cert.Thumbprint)" -ForegroundColor Green
} else {
  Write-Host "No certificate for $HostName in LocalMachine\My. HTTP is bound; install the cert, then re-run this script for https://$HostName" -ForegroundColor Yellow
}

Start-Website -Name $SiteName -ErrorAction SilentlyContinue

Write-Host "==> Enable ARR proxy (server level)..." -ForegroundColor Cyan
try {
  Set-WebConfigurationProperty -pspath 'MACHINE/WEBROOT/APPHOST' -filter "system.webServer/proxy" -name "enabled" -value "True"
} catch {
  Write-Host "Could not toggle ARR proxy automatically. In IIS: server node -> Application Request Routing Cache -> Server Proxy Settings -> Enable proxy." -ForegroundColor Yellow
}

try {
  Set-WebConfigurationProperty -pspath "MACHINE/WEBROOT/APPHOST/$SiteName" -filter "system.webServer/serverRuntime" -name "uploadReadAheadSize" -value 10485760
} catch {
  try {
    Set-WebConfigurationProperty -pspath 'MACHINE/WEBROOT/APPHOST' -filter "system.webServer/serverRuntime" -name "uploadReadAheadSize" -value 10485760
  } catch {
    Write-Host "Could not set uploadReadAheadSize. File uploads through IIS may fail until this is 10485760." -ForegroundColor Yellow
  }
}

Write-Host "==> PM2 uvicorn on 127.0.0.1:$AppPort..." -ForegroundColor Cyan
$pm2 = Get-Command pm2 -ErrorAction SilentlyContinue
if (-not $pm2) {
  npm install -g pm2
  npm install -g pm2-windows-startup
}

$eco = Join-Path $AppRoot "ecosystem.config.cjs"
$ecoBody = @"
module.exports = {
  apps: [{
    name: "job-discovery",
    script: "python",
    args: "-m uvicorn api:app --host 127.0.0.1 --port $AppPort",
    cwd: $(ConvertTo-Json $AppRoot),
    interpreter: "none",
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: "800M",
    env: { PYTHONUNBUFFERED: "1" }
  }]
};
"@
[System.IO.File]::WriteAllText($eco, $ecoBody, $utf8)

Push-Location $AppRoot
pm2 delete job-discovery 2>$null
pm2 start $eco
pm2 save
Pop-Location

try {
  pm2-startup install
} catch {
  Write-Host "pm2-startup skipped (run once as Admin if needed)" -ForegroundColor Yellow
}

Start-Sleep -Seconds 2
try {
  $health = Invoke-RestMethod -Uri "http://127.0.0.1:$AppPort/api/health" -TimeoutSec 5
  Write-Host "Uvicorn health: $($health | ConvertTo-Json -Compress)" -ForegroundColor Green
} catch {
  Write-Host "Uvicorn not responding yet on :$AppPort. Check: pm2 logs job-discovery" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "IIS site ready:" -ForegroundColor Green
Write-Host "  http://${HostName}/"
Write-Host "  https://${HostName}/"
Write-Host "  http://${ServerIP}:${Port}/api/health"
Write-Host "Uvicorn stays on localhost:$AppPort. IIS needs URL Rewrite + ARR with proxy enabled."
