# NeutralBasis V2 setup (Windows). PAPER ONLY: creates .venv, installs requirements,
# runs the offline test suite. Starts nothing.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$py = (Get-Command py -ErrorAction SilentlyContinue)
if ($py) { py -3.13 -m venv .venv } else { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -c "import sys; assert sys.version_info >= (3, 13), sys.version"
.\.venv\Scripts\python.exe -m unittest discover -p "test_*.py"
