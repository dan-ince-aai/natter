#!/bin/bash
# Natter installer: https://github.com/dan-ince-aai/natter
#
#   curl -fsSL https://dan-ince-aai.github.io/natter/install.sh | bash
#
# Downloads the latest Natter.dmg from GitHub Releases, checks its SHA-256 against the checksum published with it,
# copies Natter.app into /Applications and opens it. Natter isn't notarised by Apple yet; files downloaded with curl
# aren't marked "from the internet", so macOS opens the app without the "can't check this app" block.
set -euo pipefail

REPO="dan-ince-aai/natter"
URL="https://github.com/$REPO/releases/latest/download"
DEST="${NATTER_INSTALL_DIR:-/Applications}"

say() { printf '\033[1;32m▸\033[0m %s\n' "$1"; }
die() { printf '\033[1;31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "Natter is a Mac app."
[ "$(uname -m)" = "arm64" ] || die "Natter needs an Apple silicon Mac (M1 or later) for now."
ver="$(sw_vers -productVersion)"; major="${ver%%.*}"; minor="$(echo "$ver" | cut -d. -f2)"
{ [ "$major" -gt 14 ] || { [ "$major" -eq 14 ] && [ "${minor:-0}" -ge 4 ]; }; } || die "Natter needs macOS 14.4 or later (you have $ver)."

TMP="$(mktemp -d)"; MNT=""
cleanup() { [ -n "$MNT" ] && hdiutil detach "$MNT" -quiet 2>/dev/null; rm -rf "$TMP"; }
trap cleanup EXIT

say "Downloading Natter…"
curl -fL --progress-bar -o "$TMP/Natter.dmg" "$URL/Natter.dmg"
curl -fsSL -o "$TMP/Natter.dmg.sha256" "$URL/Natter.dmg.sha256"
say "Checking the download…"
(cd "$TMP" && shasum -a 256 -c Natter.dmg.sha256 >/dev/null) || die "Checksum mismatch: the download isn't the published Natter.dmg."

MNT="$(hdiutil attach -nobrowse -readonly -noautoopen "$TMP/Natter.dmg" | awk -F'\t' '/\/Volumes\// {print $NF}')"
[ -d "$MNT/Natter.app" ] || die "Couldn't open the disk image."

if pgrep -f "$DEST/Natter.app/Contents/MacOS/Natter" >/dev/null; then
  say "Quitting the running Natter…"
  osascript -e 'tell application "Natter" to quit' >/dev/null 2>&1 || pkill -f "$DEST/Natter.app/Contents/MacOS/Natter" || true
  sleep 2
fi

say "Installing to $DEST…"
SUDO=""; [ -w "$DEST" ] || SUDO="sudo"
$SUDO rm -rf "$DEST/Natter.app"
$SUDO ditto "$MNT/Natter.app" "$DEST/Natter.app"
$SUDO /usr/bin/xattr -dr com.apple.quarantine "$DEST/Natter.app" 2>/dev/null || true

say "Done. Opening Natter."
[ -n "${NATTER_NO_OPEN:-}" ] || open "$DEST/Natter.app"
cat <<'TIP'

  Paste your AssemblyAI key when Natter asks (get one free: https://www.assemblyai.com/dashboard/api-keys).
  To talk on calls, or turn the original down while you listen, add the free audio drivers once:
    brew install blackhole-2ch blackhole-16ch
TIP
