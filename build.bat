@echo off
cd /d "%~dp0"
echo ============================================
echo  Build QoderProxy.exe
echo ============================================
echo.
echo  Requires only: python + pyinstaller (pure stdlib, no third-party runtime deps)
echo    python -m pip install pyinstaller
echo.
echo  Building ...
echo.
python -m PyInstaller --noconfirm --onefile --noconsole --name QoderProxy main.py
echo.
echo Done. Check dist\QoderProxy.exe
pause