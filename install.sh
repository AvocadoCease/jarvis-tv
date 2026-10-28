#!/bin/bash
# One-time setup on the Mac. Safe to run again.
#
#   ./install.sh            everything: Python env, Whisper, background services, the Mac app
#   ./install.sh --no-mac-app   skip dictation + "ask JARVIS" (TV and phone remote only)
#
# Before running: copy config.example.json to config.json and fill it in (see README).
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
MAC_APP=1
[ "$1" = "--no-mac-app" ] && MAC_APP=0

[ -f "$ROOT/config.json" ] || { cp "$ROOT/config.example.json" "$ROOT/config.json";
  echo "Created config.json from the example. Fill it in, then run ./install.sh again."; exit 1; }
command -v brew >/dev/null || { echo "Needs Homebrew: https://brew.sh"; exit 1; }
command -v claude >/dev/null || [ -x "$HOME/.local/bin/claude" ] || echo "Note: Claude Code isn't installed; JARVIS will only do the built-in TV commands until it is."

echo "== Python environment"
[ -x "$ROOT/.venv/bin/python" ] || python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt"
brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
mkdir -p "$HOME/Library/Logs/jarvis-tv" "$HOME/.config/lounge" "$HOME/.config/jarvis"

render() {  # template -> ~/Library/LaunchAgents, then (re)load it
  local src="$ROOT/launchd/$1.plist" label="com.jarvis-tv.$1" dst="$AGENTS/com.jarvis-tv.$1.plist"
  sed -e "s#__ROOT__#$ROOT#g" -e "s#__HOME__#$HOME#g" -e "s#__WHISPER__#${WHISPER:-}#g" \
      -e "s#__PROMPT__#${PROMPT:-}#g" "$src" > "$dst"
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$dst"
  echo "   started $label"
}

echo "== JARVIS server (port 80)"
render server

if [ $MAC_APP = 1 ]; then
  echo "== Whisper (local speech-to-text)"
  brew list whisper-cpp >/dev/null 2>&1 || brew install whisper-cpp
  WHISPER="$(command -v whisper-server || echo /opt/homebrew/bin/whisper-server)"
  MODEL="$ROOT/mac-app/models/ggml-large-v3-turbo-q5_0.bin"
  if [ ! -f "$MODEL" ]; then
    mkdir -p "$(dirname "$MODEL")"
    curl -L --fail -o "$MODEL" https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin
  fi
  PROMPT="$("$ROOT/.venv/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("whisper_prompt","JARVIS."))' "$ROOT/config.json" | sed 's/[#&]//g')"
  render whisper

  echo "== Jarvis.app"
  [ -f "$HOME/.config/jarvis/replacements.json" ] || cp "$ROOT/mac-app/replacements.example.json" "$HOME/.config/jarvis/replacements.json"
  "$ROOT/mac-app/build.sh"
  sed -e "s#__HOME__#$HOME#g" "$ROOT/launchd/mac-app.plist" > "$AGENTS/com.jarvis-tv.mac-app.plist"
  echo "   Jarvis.app opens at login. Grant it Microphone, Accessibility and Input Monitoring when asked."
fi

echo
echo "Done. Check it:  ./jarvis status     Phone remote:  http://<this Mac's IP>/remote"
