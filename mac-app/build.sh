#!/bin/bash
# Build Jarvis.app (dictation + "ask JARVIS"), install it to ~/Applications and launch it.
#
# macOS ties its permissions (Microphone, Accessibility, Input Monitoring) to the app's code
# signature. Signed ad hoc, every rebuild looks like a new app and the permissions silently stop
# working. So if you have a signing identity called "Jarvis Local Signing" (see README: one-time,
# Keychain Access > Certificate Assistant > Create a Certificate, type Code Signing), it's used
# and the permissions survive rebuilds.
set -e
cd "$(dirname "$0")"
swiftc -O -swift-version 5 Sources/main.swift -o Jarvis.app/Contents/MacOS/Jarvis -framework Cocoa -framework AVFoundation
if security find-identity -p codesigning | grep -q "Jarvis Local Signing"; then
  codesign --force --sign "Jarvis Local Signing" --identifier com.jarvis-tv.mac Jarvis.app
else
  echo "No 'Jarvis Local Signing' identity: signing ad hoc (re-grant permissions after each rebuild)"
  codesign --force --sign - --identifier com.jarvis-tv.mac Jarvis.app
fi
pkill -x Jarvis 2>/dev/null || true
for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -x Jarvis >/dev/null || break; sleep 0.3; done
mkdir -p ~/Applications ~/.config/jarvis
rm -rf ~/Applications/Jarvis.app
cp -R Jarvis.app ~/Applications/
open ~/Applications/Jarvis.app
echo "Jarvis built, installed to ~/Applications and launched"
