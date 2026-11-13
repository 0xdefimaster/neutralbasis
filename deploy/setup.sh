#!/bin/sh
# NeutralBasis V2 setup for Linux / macOS. PAPER ONLY: creates .venv, installs
# requirements.txt, runs the offline test suite. Starts nothing, installs no system
# package by itself and never uses sudo.
#
# Ubuntu 24.04 ships Python 3.12; the suite is verified on 3.13 only. Get 3.13 first:
#   sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt update
#   sudo apt install python3.13 python3.13-venv
# or with uv:  curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.13
# Then:  PYTHON=python3.13 sh deploy/setup.sh   (or the path uv prints)
set -eu
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3.13}"
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "Python 3.13 not found ($PY). See the comments at the top of this file." >&2
    exit 2
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)'; then
    echo "$PY is $("$PY" -V 2>&1); Python 3.13 is required (verified version)." >&2
    exit 2
fi
if ! "$PY" -c 'import ensurepip, venv' >/dev/null 2>&1; then
    echo "venv/ensurepip missing: on Debian/Ubuntu install python3.13-venv." >&2
    exit 2
fi
"$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -p "test_*.py"
