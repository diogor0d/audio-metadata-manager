param(
    [switch]$RemoveData
)

$ErrorActionPreference = "Stop"
$ProgramsShortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Liner.lnk"
$StartupShortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "Liner.lnk"
Remove-Item -LiteralPath $ProgramsShortcut -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $StartupShortcut -Force -ErrorAction SilentlyContinue

$DataDirectory = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "Liner"
$PidFile = Join-Path $DataDirectory "liner.pid"
if (Test-Path -LiteralPath $PidFile) {
    $ProcessId = [int](Get-Content -LiteralPath $PidFile -Raw)
    $Process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if ($Process -and $Process.Name -match '^python(?:\.exe)?$' -and $Process.CommandLine -match '(?i)-m\s+liner\.cli(?:\s|$)') {
        Stop-Process -Id $ProcessId -ErrorAction Stop
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

if ($RemoveData) {
    if (Test-Path -LiteralPath $DataDirectory) {
        Remove-Item -LiteralPath $DataDirectory -Recurse -Force
    }
}

Write-Host "Liner shortcuts and local process were removed. Runtime data was preserved unless -RemoveData was supplied."
