@echo off
rem Kayitlarin durum raporu (API'ye baglanmaz).
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m tools.phase1_recorder --status %*
pause
