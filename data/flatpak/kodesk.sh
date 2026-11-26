#!/bin/sh
# Flatpak launcher: the package sits in /app/share/kodesk, PySide6 comes from the PySide BaseApp.
export PYTHONPATH=/app/share/kodesk${PYTHONPATH:+:$PYTHONPATH}
exec python3 -m kodesk "$@"
