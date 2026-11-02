Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$runtimePython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtimePython)) {
    & (Join-Path $PSScriptRoot 'install.ps1')
}
$windowPython = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$scriptPath = Join-Path $PSScriptRoot 'setup_gui.py'
Start-Process -FilePath $windowPython -ArgumentList @('"' + $scriptPath + '"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
