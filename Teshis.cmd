@echo off
if exist "%~dp0.venv\Scripts\python.exe" ("%~dp0.venv\Scripts\python.exe" "%~dp0teshis.py" %*) else (python "%~dp0teshis.py" %*)
pause
