@echo off
setlocal
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
%PY% "%~dp0..\..\tools\psbuild.py" build "%~dp0." %*
if errorlevel 1 pause
