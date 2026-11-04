@echo off
rem Phase 1 yerel piyasa kaniti kaydi. Yalnizca public veri okur; emir gondermez,
rem paper motoruna ve veritabanina dokunmaz. Ctrl+C veya Phase1-Durdur.cmd ile durur.
rem Argumansiz: 24 saatlik kayit. Ornek: Phase1-Kayit.cmd --once  /  --duration-hours 72
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
if "%~1"=="" (
  "%PY%" -m tools.phase1_recorder --duration-hours 24
) else (
  "%PY%" -m tools.phase1_recorder %*
)
pause
