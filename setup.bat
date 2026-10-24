@echo off
rem One-time setup: remembers where PocketSim DevKit is and checks your tools.
setlocal
cd /d "%~dp0"
echo PocketSim DevKit setup
echo.
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3" & goto havepy)
where python >nul 2>nul
if %errorlevel%==0 (set "PY=python" & goto havepy)
echo Python 3 was not found. Install it, then run setup.bat again:
echo     winget install Python.Python.3.12
echo or download it from https://www.python.org/downloads/  (tick "Add python.exe to PATH")
pause
exit /b 1
:havepy
%PY% tools\psbuild.py setup
echo.
pause
