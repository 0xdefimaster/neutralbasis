@echo off
rem Veri platformu yetenek matrisi ve kanonik piyasalar. Yalnizca phase1-evidence
rem klasorunu okur; borsaya baglanmaz, motora ve kayda dokunmaz.
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m marketdata %*
pause
