#!/bin/bash
set -e
project_dir="$(cd "$(dirname "$0")" && pwd)"
cd "$project_dir"
if [ -x ".venv/bin/python" ]; then
  exec ".venv/bin/python" gui.py
fi
exec python3 gui.py

