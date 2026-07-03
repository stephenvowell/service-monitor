# Build a standalone Windows executable for Service Health Monitor.
# Requires: pip install pyinstaller
#
# Usage:
#   ./build_exe.ps1
#
# Output: dist/ServiceHealthMonitor.exe

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

python -m PyInstaller --noconfirm --clean `
    --name ServiceHealthMonitor `
    --onefile `
    --windowed `
    service_monitor.py

Write-Host ""
Write-Host "Done. Executable: $PSScriptRoot\dist\ServiceHealthMonitor.exe"
