#!/bin/bash
# Builds dist/Natter.dmg: a self-contained Natter.app (its own Python, the engine and the Swift helpers) for Apple
# silicon Macs. Nothing outside the app is needed except the BlackHole drivers for Talk and for muting.
#   ./package.sh            -> dist/Natter.app and dist/Natter.dmg
# Signing: ad-hoc by default. Set SIGN_ID="Developer ID Application: …" to sign for distribution (then notarise).
set -euo pipefail
cd "$(dirname "$0")"
VERSION="${VERSION:-0.1.1}"
PY_TAG=20261020
PY_NAME="cpython-3.12.15+${PY_TAG}-aarch64-apple-darwin-install_only.tar.gz"
PY_SHA256=921fc822416bbad146b026d2051dbfe08338ce4144ced64c56afd899a4351752
SIGN_ID="${SIGN_ID:--}"

CACHE="${TMPDIR:-/tmp}/natter-pkg-cache"; mkdir -p "$CACHE"
if [ ! -f "$CACHE/$PY_NAME" ]; then
  curl -fL -o "$CACHE/$PY_NAME" "https://github.com/astral-sh/python-build-standalone/releases/download/${PY_TAG}/${PY_NAME//+/%2B}"
fi
echo "$PY_SHA256  $CACHE/$PY_NAME" | shasum -a 256 -c - >/dev/null || { echo "Python download checksum mismatch"; exit 1; }

OUT=dist; APP="$OUT/Natter.app"; RES="$APP/Contents/Resources"
rm -rf "$OUT"; mkdir -p "$APP/Contents/MacOS" "$RES/app/bin"
swiftc -O -swift-version 5 -o "$RES/app/bin/syscap" syscap/main.swift
swiftc -O -swift-version 5 -o "$RES/app/bin/audiodev" audiodev/main.swift
swiftc -O -swift-version 5 -o "$APP/Contents/MacOS/Natter" launcher/main.swift
cp app.py "$RES/app/"; cp -R static "$RES/app/static"
cp assets/AppIcon.icns assets/menubar.png assets/menubar@2x.png assets/menubar-live.png assets/menubar-live@2x.png "$RES/"

tar -xzf "$CACHE/$PY_NAME" -C "$RES"            # -> $RES/python
"$RES/python/bin/python3" -m pip install --quiet --disable-pip-version-check --no-cache-dir --only-binary=:all: -r requirements-lock.txt
find "$RES/python" -type d \( -name __pycache__ -o -name tests -o -name test \) -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$RES/python/lib/python3.12/idlelib" "$RES/python/lib/python3.12/tkinter" "$RES/python/lib/python3.12/turtledemo"

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
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>14.4</string>
  <key>LSUIElement</key><true/>
  <key>LIBundled</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Natter listens to your microphone to translate what you say.</string>
  <key>NSAudioCaptureUsageDescription</key><string>Natter listens to what your Mac plays, such as a call, to translate it for you.</string>
</dict></plist>
PLIST

# Sign every binary inside first, then the app.
find "$RES" -type f \( -name "*.so" -o -name "*.dylib" -o -perm -u+x \) -print0 | while IFS= read -r -d '' f; do
  file -b "$f" | grep -q "Mach-O" && codesign --force --timestamp=none --sign "$SIGN_ID" "$f" >/dev/null 2>&1 || true
done
codesign --force --timestamp=none --sign "$SIGN_ID" "$APP"

# The DMG opens as a pixel-art window: Natter on the left, Applications on the right, "drag Natter into Applications".
# dmgbuild writes the window layout (.DS_Store) directly, no Finder scripting. Pinned, in its own venv.
DMGVENV="$CACHE/dmgbuild-venv"
[ -x "$DMGVENV/bin/dmgbuild" ] || { "$(command -v python3)" -m venv "$DMGVENV" && "$DMGVENV/bin/pip" install --quiet --disable-pip-version-check "dmgbuild==1.6.5"; }
"$DMGVENV/bin/dmgbuild" -s packaging/dmg_settings.py -D app="$APP" "Natter" "$OUT/Natter.dmg" >/dev/null
echo "Built $OUT/Natter.dmg ($(du -h "$OUT/Natter.dmg" | cut -f1)), app $(du -sh "$APP" | cut -f1)"
