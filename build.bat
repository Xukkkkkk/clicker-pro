@echo off
cd /d "%~dp0"
python build.py
set "clicker_build_result=%errorlevel%"
pause
exit /b %clicker_build_result%
