#!/bin/sh
# NeutralBasis V2 setup (Linux/macOS, NOT yet verified). PAPER ONLY: creates .venv,
# installs requirements, runs the offline test suite. Starts nothing.
set -e
cd "$(dirname "$0")/.."
python3.13 -m venv .venv 2>/dev/null || python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -c "import sys; assert sys.version_info >= (3, 13), sys.version"
.venv/bin/python -m unittest discover -p "test_*.py"
