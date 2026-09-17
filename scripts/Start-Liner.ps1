param(
    [switch]$OpenBrowser
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Executable = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$EnvironmentFile = Join-Path $ProjectRoot ".env"
$Port = 8764
if ($env:LINER_PORT) {
    $Port = [int]$env:LINER_PORT
} elseif (Test-Path -LiteralPath $EnvironmentFile) {
    $PortLine = Get-Content -LiteralPath $EnvironmentFile | Where-Object { $_ -match '^\s*LINER_PORT\s*=\s*\d+\s*$' } | Select-Object -Last 1
    if ($PortLine -and $PortLine -match '=\s*(\d+)\s*$') {
        $Port = [int]$Matches[1]
    }
}
$Url = "http://127.0.0.1:$Port"
$DataDirectory = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "Liner"
$PidFile = Join-Path $DataDirectory "liner.pid"

if (-not (Test-Path -LiteralPath $Executable)) {
    throw "Liner is not installed. Run scripts\Install-Liner.ps1 first."
}

$Listening = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($Listening) {
    try {
        $ExistingHealth = Invoke-RestMethod -Uri "$Url/health" -TimeoutSec 2
    } catch {
        throw "Port $Port is occupied by another application."
    }
    if ($ExistingHealth.status -ne "ok" -or $ExistingHealth.version -ne "1.0.0") {
        throw "Port $Port is occupied by another application."
    }
    New-Item -ItemType Directory -Path $DataDirectory -Force | Out-Null
    Set-Content -LiteralPath $PidFile -Value $Listening.OwningProcess -Encoding ASCII
} else {
    New-Item -ItemType Directory -Path $DataDirectory -Force | Out-Null
    $Process = Start-Process -FilePath $Executable -ArgumentList "-m", "liner.cli", "--no-browser" -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru
    $Deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 250
        try {
            $Healthy = (Invoke-RestMethod -Uri "$Url/health" -TimeoutSec 1).status -eq "ok"
        } catch {
            $Healthy = $false
        }
    } while (-not $Healthy -and (Get-Date) -lt $Deadline)
    if (-not $Healthy) {
        Stop-Process -Id $Process.Id -ErrorAction SilentlyContinue
        throw "Liner did not become healthy within 20 seconds."
    }
    $LinerListener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction Stop
    Set-Content -LiteralPath $PidFile -Value $LinerListener.OwningProcess -Encoding ASCII
}

if ($OpenBrowser) {
    Start-Process $Url
}
