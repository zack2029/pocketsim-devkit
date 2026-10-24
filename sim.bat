@echo off
rem Run a .bin / .mcs / .mcd in pocketsim:  sim game.bin
setlocal
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
%PY% "%~dp0tools\pocketsim.py" %*
