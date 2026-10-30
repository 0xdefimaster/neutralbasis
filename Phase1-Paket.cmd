@echo off
rem Dogrulama yapar ve tum kayitlari tek ZIP paketine koyar (phase1-evidence\exports).
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m tools.phase1_recorder --validate-only %*
"%PY%" -m tools.phase1_recorder --export %*
pause
