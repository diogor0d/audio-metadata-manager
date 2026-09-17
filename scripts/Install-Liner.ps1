param(
    [switch]$NoStartup,
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$FrontendRoot = Join-Path $ProjectRoot "frontend"
$StartScript = Join-Path $PSScriptRoot "Start-Liner.ps1"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required. Install it from https://docs.astral.sh/uv/."
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "Node.js and npm are required to build the interface."
}

Push-Location $ProjectRoot
try {
    uv sync --frozen
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed with exit code $LASTEXITCODE." }
    Push-Location $FrontendRoot
    try {
        npm ci
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed with exit code $LASTEXITCODE." }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "npm build failed with exit code $LASTEXITCODE." }
    } finally {
        Pop-Location
    }
} finally {
    Pop-Location
}

$Shell = New-Object -ComObject WScript.Shell
$Programs = [Environment]::GetFolderPath("Programs")
$MenuShortcut = $Shell.CreateShortcut((Join-Path $Programs "Liner.lnk"))
$MenuShortcut.TargetPath = "powershell.exe"
$MenuShortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`" -OpenBrowser"
$MenuShortcut.WorkingDirectory = $ProjectRoot
$MenuShortcut.Description = "Open the Liner local-file workbench"
$MenuShortcut.Save()

if (-not $NoStartup) {
    $Startup = [Environment]::GetFolderPath("Startup")
    $StartupShortcut = $Shell.CreateShortcut((Join-Path $Startup "Liner.lnk"))
    $StartupShortcut.TargetPath = "powershell.exe"
    $StartupShortcut.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$StartScript`""
    $StartupShortcut.WorkingDirectory = $ProjectRoot
    $StartupShortcut.Description = "Start Liner locally at sign-in"
    $StartupShortcut.Save()
}

if (-not $NoLaunch) {
    & $StartScript -OpenBrowser
}

Write-Host "Liner is installed for the current user. Use the Start Menu shortcut to open it."
