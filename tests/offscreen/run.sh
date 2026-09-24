#!/bin/sh
# Run the offscreen plugin test with Krita's bundled PyQt5 under a CPython 3.13.
set -e
KRITA=${KRITA:-/Applications/krita.app}
PY=${PY:-$(uv python find 3.13)}
export QT_QPA_PLATFORM=offscreen
export QT_PLUGIN_PATH="$KRITA/Contents/PlugIns"
export DYLD_FRAMEWORK_PATH="$KRITA/Contents/Frameworks"
export PYTHONPATH="$KRITA/Contents/Frameworks/Python.framework/Versions/3.13/lib/python3.13/site-packages"
exec "$PY" "$(dirname "$0")/run_offscreen.py" "$@"
