# Build QoderProxy.exe (single-file, no console)
# Run from cmd (NOT double-click; double-click opens notepad):
#   powershell -ExecutionPolicy Bypass -File build.ps1
# Requires: Python 3.10+ with "Add to PATH" + pyinstaller (pure stdlib, no third-party runtime deps)

Set-Location -Path $PSScriptRoot

Write-Host "Building QoderProxy.exe ..."
Write-Host "Requires only python + pyinstaller:"
Write-Host "  python -m pip install pyinstaller"
Write-Host ""

python -m PyInstaller --noconfirm --onefile --noconsole --name QoderProxy main.py

Write-Host ""
Write-Host "Done. Check dist\QoderProxy.exe"
Read-Host "Press Enter to exit"