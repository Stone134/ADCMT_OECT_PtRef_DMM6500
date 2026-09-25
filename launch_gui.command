#!/bin/bash
set -e
project_dir="$(cd "$(dirname "$0")" && pwd)"
cd "$project_dir"
if [ -x ".venv/bin/python" ]; then
  # Qt ignores plugins carrying the macOS hidden flag.
  for plugin_dir in "$project_dir"/.venv/lib/python*/site-packages/PyQt5/Qt5/plugins; do
    if [ -d "$plugin_dir" ]; then
      chflags -R nohidden "$plugin_dir"
    fi
  done
  exec ".venv/bin/python" gui.py
fi
exec python3 gui.py

