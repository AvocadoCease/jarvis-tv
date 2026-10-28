"""JARVIS's speaking voice: macOS `say` with a light "in-suit" audio treatment.

A stock British voice, not a clone of anyone's real voice. Prefers a downloaded
premium/enhanced voice if one is installed (System Settings > Accessibility >
Spoken Content > System Voice > Manage Voices), else Daniel.

  synth("Volume set to twenty.") -> path to an .mp3 (cached by text)
"""
import hashlib
import os
import re
import subprocess

CACHE = "/tmp/lounge-voice"
PREFERRED = ["Jamie (Premium)", "Oliver (Premium)", "Daniel (Premium)", "Jamie (Enhanced)",
             "Oliver (Enhanced)", "Daniel (Enhanced)", "Daniel"]
RATE = 182  # words per minute: measured, unhurried

# High/low-pass for a speaker-in-the-room tone, a faint 35 ms slapback for "space",
# and gentle compression so it stays clear at low volume.
FILTER = ("highpass=f=110,lowpass=f=7800,"
          "aecho=0.85:0.5:35:0.12,"
          "acompressor=threshold=-18dB:ratio=3:attack=5:release=120,"
          "volume=1.6")

_voice = {"name": None}


def voice_name():
    if _voice["name"]:
        return _voice["name"]
    listing = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
    names = [re.split(r"\s{2,}", line)[0].strip() for line in listing.splitlines()]
    _voice["name"] = next((v for v in PREFERRED if v in names), "Daniel")
    return _voice["name"]


def synth(text):
    text = (text or "").strip()[:1500]
    os.makedirs(CACHE, exist_ok=True)
    key = hashlib.sha1(f"{voice_name()}|{RATE}|{FILTER}|{text}".encode()).hexdigest()[:16]
    out = os.path.join(CACHE, key + ".mp3")
    if os.path.exists(out):
        return out
    raw = os.path.join(CACHE, key + ".aiff")
    subprocess.run(["say", "-v", voice_name(), "-r", str(RATE), "-o", raw, text], check=True, timeout=60)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", raw, "-af", FILTER,
                    "-ar", "44100", "-ac", "1", "-b:a", "128k", out], check=True, timeout=30)
    os.remove(raw)
    return out


if __name__ == "__main__":
    import sys
    print(synth(" ".join(sys.argv[1:]) or "Good afternoon. All systems are online."))
