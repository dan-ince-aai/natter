#!/bin/bash
# Builds bin/syscap (system audio tap) and "Natter.app" (menu bar launcher) for this folder.
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
PY="${PYTHON:-$(command -v python3)}"
PY="$("$PY" -c 'import sys; print(sys.executable)')"
"$PY" -c 'import sounddevice, numpy, scipy, websockets, aiohttp' 2>/dev/null || "$PY" -m pip install -r requirements.txt

mkdir -p bin
swiftc -O -swift-version 5 -o bin/syscap syscap/main.swift
swiftc -O -swift-version 5 -o bin/audiodev audiodev/main.swift

APP="$DIR/Natter.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -O -swift-version 5 -o "$APP/Contents/MacOS/Natter" launcher/main.swift
cp assets/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
cp assets/menubar.png assets/menubar@2x.png assets/menubar-live.png assets/menubar-live@2x.png "$APP/Contents/Resources/"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Natter</string>
  <key>CFBundleDisplayName</key><string>Natter</string>
  <key>CFBundleIdentifier</key><string>dev.natter.app</string>
  <key>CFBundleExecutable</key><string>Natter</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.4</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Natter listens to your microphone to translate what you say.</string>
  <key>NSAudioCaptureUsageDescription</key><string>Natter listens to what your Mac plays, such as a call, to translate it for you.</string>
  <key>LIProjectDir</key><string>$DIR</string>
  <key>LIPython</key><string>$PY</string>
</dict></plist>
PLIST
codesign --force --sign - "$APP" >/dev/null 2>&1 || true
echo "Built: $APP  (python: $PY)"
