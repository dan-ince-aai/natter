#!/bin/bash
# Dev mode: run the engine from a terminal. macOS will ask for mic / system-audio permission for your terminal app.
# For normal use, open "Natter.app" instead (run ./build.sh first).
cd "$(dirname "$0")"
[ -x bin/syscap ] || ./build.sh
exec python3 -u app.py "$@"
