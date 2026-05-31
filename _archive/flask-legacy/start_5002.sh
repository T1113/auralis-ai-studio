#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

. .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
pip install -r requirements.txt

export PORT=5002
python app.py
