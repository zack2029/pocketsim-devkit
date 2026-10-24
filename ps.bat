@echo off
rem PocketSim DevKit command line:  ps build ^| run ^| new ^| info ^| install ^| img2c ^| doctor ^| setup
setlocal
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
%PY% "%~dp0tools\psbuild.py" %*
