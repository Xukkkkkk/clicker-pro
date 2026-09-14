@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
python -m PyInstaller --clean --noconfirm ClickerPro.spec
echo Build complete: dist\ClickerPro.exe
pause
