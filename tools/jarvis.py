"""Turn a spoken or typed command into TV actions.

handle("jarvis, volume up 5") -> {"ok": True, "reply": "Volume up 5.", "did": [...]}

Rule-based on purpose for the home: fast, offline, predictable. Questions and
anything the rules don't recognise go to the brain (Claude, see brain.py).
"""
import json
import os
import random
import re
import threading
import time
import urllib.request

import brain
import briefing
import settings
import tvcontrol
import xboxlocal as xbox
try:
    import xboxcloud  # Microsoft-linked control; needs the project's .venv
except ImportError:
    xboxcloud = None

XBOX_HDMI = int(settings.XBOX.get("hdmi") or 3)
USER_NAME = settings.USER_NAME

PHOTO_DIR = "/tmp/lounge-cast"
# Remote presses that open Lounge from anywhere. Tuned to where Lounge sits in Smart Hub;
# edit tools/lounge-launch.json to change without touching code.
LAUNCH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lounge-launch.json")
DEFAULT_LAUNCH = ["KEY_CONTENTS", "WAIT:7", "KEY_ENTER"]
NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
           "eight": 8, "nine": 9, "ten": 10, "a couple": 2, "a few": 3, "a bit": 3, "a lot": 8}

# Recent commands, shown in the TV's JARVIS log via /stats
_events = []
_events_lock = threading.Lock()
_next_id = [1]


# Set by serve.py so the briefing can mention the Mac's battery/memory.
get_stats = lambda: {}  # noqa: E731

# The latest briefing, shown on the TV via /stats
_briefing = {"id": 0, "at": 0.0, "title": "", "lines": []}


# When JARVIS is talking (set when its voice is fetched), so the TV can animate along.
_speaking = {"until": 0.0}


def mark_speaking(seconds):
    _speaking["until"] = max(_speaking["until"], time.time()) + seconds


def speaking_ms():
    return max(0, int((_speaking["until"] - time.time()) * 1000))


def current_briefing():
    if not _briefing["id"]:
        return None
    return dict(_briefing, ageSec=round(time.time() - _briefing["at"]))


def events():
    with _events_lock:
        return list(_events)


def _event(text, kind="echo"):
    """kind "echo" repeats a reply on the TV; "notice" is news the remote should also say aloud."""
    with _events_lock:
        _events.append({"id": _next_id[0], "text": text, "kind": kind, "at": int(time.time() * 1000)})
        _next_id[0] += 1
        del _events[:-5]


def _count(text, default):
    m = re.search(r"\b(\d+)", text)
    if m:
        return max(0, min(100, int(m.group(1))))
    for word, n in NUMBERS.items():
        if re.search(r"\b" + word + r"\b", text):
            return n
    return default


def _photo(server):
    os.makedirs(PHOTO_DIR, exist_ok=True)
    raw = os.path.join(PHOTO_DIR, "raw.jpg")
    out = os.path.join(PHOTO_DIR, "photo.jpg")
    urllib.request.urlretrieve(f"https://picsum.photos/1920/1080.jpg?t={time.time()}", raw)
    # old TVs need baseline (not progressive) JPEG
    import subprocess
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", raw, "-q:v", "2", out], check=True, timeout=30)
    tvcontrol.play_url(f"{server}/cast/photo.jpg?t={int(time.time())}", title="Photo", kind="image")


def _confirm_xbox_wake():
    """Wake packets get no reply, so check back and report in the TV log."""
    def check():
        if xbox.wait_online(35):
            _event("\u203a The Xbox is awake.", "notice")
        else:
            _event(f"\u203a I'm afraid the Xbox didn't wake, {USER_NAME}.", "notice")
    threading.Thread(target=check, daemon=True).start()


def _cloud():
    return xboxcloud is not None and xboxcloud.signed_in()


def _xbox_wake():
    """Wake via Microsoft when linked (works on this console), else the local packet."""
    if _cloud():
        xboxcloud.wake()
    else:
        xbox.power_on()


def _xbox_actions():
    c = xboxcloud
    return {
        "a": ("A", lambda: c.button("a")), "b": ("B", lambda: c.button("b")),
        "x": ("X", lambda: c.button("x")), "y": ("Y", lambda: c.button("y")),
        "select": ("A", lambda: c.button("a")), "ok": ("A", lambda: c.button("a")),
        "up": ("up", lambda: c.button("up")), "down": ("down", lambda: c.button("down")),
        "left": ("left", lambda: c.button("left")), "right": ("right", lambda: c.button("right")),
        "menu": ("menu", lambda: c.button("menu")), "view": ("view", lambda: c.button("view")),
        "guide": ("guide", lambda: c.button("guide")),
        "home": ("home", lambda: c.home()), "back": ("back", lambda: c.back()),
        "pause": ("pause", lambda: c.media("pause")), "play": ("play", lambda: c.media("play")),
        "next": ("next", lambda: c.media("next")), "previous": ("previous", lambda: c.media("previous")),
        "unmute": ("unmute", lambda: c.media("unmute")), "mute": ("mute", lambda: c.media("mute")),
    }


def _xbox_cloud_command(t):
    """Xbox commands that need the Microsoft link. Returns (reply, did) or None."""
    on_xbox = re.search(r"\bon\s+(?:the\s+|my\s+)?xbox$", t)
    m = re.search(r"\b(?:open|launch|start)\s+(.+?)(?:\s+on\s+(?:the\s+|my\s+)?xbox)?$", t) or \
        re.search(r"\bplay\s+(.+?)\s+on\s+(?:the\s+|my\s+)?xbox$", t)
    if m and m.group(1) not in ("xbox", "the xbox", "my xbox"):
        # "open X" without "on the Xbox" only goes to the Xbox if X is installed there
        if on_xbox or xboxcloud.find_app(m.group(1)):
            name = xboxcloud.launch(m.group(1))
            return f"Opening {name} on the Xbox.", ["xbox_launch"]
    if "xbox" not in t:
        return None
    if re.search(r"\b(what|which|list)\b.*\b(apps|applications|games)\b", t):
        names = [xboxcloud.spoken_name(a["name"]) for a in xboxcloud.installed_apps()
                 if not re.search(r"avatar|rewards", a["name"], re.I)]
        return (f"You have {len(names)} on the Xbox: " + ", ".join(names[:-1]) + f", and {names[-1]}.", [])
    m = re.search(r"\bvolume (up|down)\b", t)
    if m:
        n = max(1, min(10, _count(t, 1)))
        for _ in range(n):
            xboxcloud.volume(up=m.group(1) == "up")
        return f"Xbox volume {m.group(1)} {n}.", ["xbox_volume"]
    actions = _xbox_actions()
    for w in re.findall(r"[a-z]+", t.replace("xbox", " ")):
        if w in actions:
            label, fn = actions[w]
            times = max(1, min(10, _count(t, 1))) if label in ("up", "down", "left", "right") else 1
            for _ in range(times):
                fn()
            return f"Xbox {label}{f' {times} times' if times > 1 else ''}.", ["xbox_button"]
    return None


def _switch_input(name, fallback_key):
    """Use the TV's own input switch (exact); fall back to the remote button.

    While a Smart Hub app (e.g. Lounge) has the screen, the TV ignores input
    changes and its source list stops answering, so close the app first.
    """
    did = []
    if tvcontrol.app_in_front():
        did += _keys(["KEY_EXIT"])
        time.sleep(2.5)
    for attempt in range(2):
        try:
            tvcontrol.set_source(name)
            return did + [f"source={name}"]
        except Exception:
            time.sleep(1.5)
    return did + _keys([fallback_key])


def _launch_lounge():
    try:
        with open(LAUNCH_FILE) as f:
            cfg = json.load(f)
        seq, gap = cfg["keys"], float(cfg.get("gap", 0.6))
    except (OSError, ValueError, KeyError):
        seq, gap = DEFAULT_LAUNCH, 0.6
    tvcontrol.keys(seq, gap=gap)  # Smart Hub drops presses that come too fast
    return seq


# Words that mean the request is about the TV/Xbox/Mac, so the instant rules should handle it.
DEVICE_WORDS = (r"\b(tv|television|xbox|volume|loud|louder|quieter|softer|mute|unmute|hdmi|channel|input|source|"
                r"screen|cast|casting|mirror|lounge|smart ?hub|photo|wallpaper|picture)\b")
# How questions and requests for information start.
QUESTION = (r"^(what|what's|whats|who|who's|when|where|why|how|which|whose|any|anything|is there|are there|"
            r"do i|did|does|have i|has|have|tell me|read|summari[sz]e|check|find|search|look up|give me|list|"
            r"show me my|remind|can i|should i|am i|will i|catch me up|anything new)\b")


def _wants_brain(raw, t):
    """A question about the world/work (calendar, email, Jira...) rather than a home command."""
    if re.search(DEVICE_WORDS, raw.lower()):
        return False
    return bool(re.search(QUESTION, t)) or len(t.split()) > 6


def _answer_card(question, reply, source):
    """Put the answer on the TV as a card, when it was asked from the phone or TV."""
    if source == "mac":
        return
    lines = [x.strip() for x in re.split(r"(?<=[.!?])\s+", reply) if x.strip()]
    title = question.strip().rstrip("?.!").upper()
    title = title if len(title) <= 48 else title[:46].rstrip() + "\u2026"
    _briefing.update(id=_briefing["id"] + 1, at=time.time(), title=title, lines=lines[:8])


def _ask_brain(raw, source):
    answer = brain.ask(raw, source=source)
    if answer.get("confirm"):  # an action waiting for a yes: show exactly what it will do
        if source != "mac":
            _briefing.update(id=_briefing["id"] + 1, at=time.time(), title="SAY YES TO CONFIRM",
                             lines=[answer["reply"]] + answer["details"].split("\n")[:6])
        return {"ok": True, "reply": answer["reply"], "did": ["brain"], "confirm": True,
                "details": answer["details"], "seconds": answer.get("seconds")}
    if answer["ok"]:
        _answer_card(raw, answer["reply"], source)
    return {"ok": answer["ok"], "reply": answer["reply"], "did": ["brain"], "seconds": answer.get("seconds")}


def _keys(names):
    tvcontrol.keys(names)
    return names


def handle(text, server, source="remote"):
    """Run one command or answer a question.

    `server` is this Mac's base URL as the TV sees it; `source` is where it was asked
    ("mac", "remote", "tv"), which decides where the answer is shown.
    """
    raw = (text or "").strip()
    t = raw.lower()
    t = re.sub(r"^(hey |ok |okay )?jarvis[,.!]?\s*", "", t)
    t = re.sub(r"\b(please|could you|can you|would you|the tv's|the tv|tv's)\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .!?")
    if not t:
        return {"ok": False, "reply": f"Yes, {USER_NAME}? What can I do for you?"}

    if brain.pending():  # "yes", "no" or a change to the action JARVIS just asked about
        return _ask_brain(raw, source)

    reply, did = None, None
    short = len(t.split()) <= 4  # bare navigation words ("up", "go back") only count in short commands
    try:
        if re.search(r"\bprivacy( mode)?\b|\bguests? (are )?(here|over|coming)\b|\bguests? (have )?(left|gone)\b", t):
            on = not re.search(r"\b(off|disable|stop|end|left|gone)\b", t)
            brain.set_privacy(on)
            return {"ok": True, "did": ["privacy"], "reply": (
                "Privacy mode on. I won't read messages out on the TV." if on else
                "Privacy mode off. I'll read messages on the TV again.")}
        if (re.search(r"\bannouncements?\b", t) and re.search(r"\b(on|off|enable|disable|stop|start|mute|resume|pause)\b", t)
                or re.search(r"\bdo not disturb\b|\bdon'?t disturb me\b|\bquiet mode\b", t)):
            on = bool(re.search(r"\b(on|enable|start|resume|back)\b", t)) and not re.search(r"\bdo not disturb\b|\bdon'?t disturb\b|\bquiet\b", t)
            brain.set_setting("announce", on)
            return {"ok": True, "did": ["announce"], "reply": (
                "Announcements on. I'll tell you when sessions finish or need you, and before meetings." if on else
                "Understood. I'll keep quiet until you turn announcements back on.")}
        if re.search(r"\bbrief(ing| me)\b|\bgood (morning|afternoon|evening)\b|\bstatus report\b|\bwhat'?s (the )?(update|news)\b", t):
            stats = get_stats()
            for _ in range(8):  # right after a restart the first sample takes a few seconds
                if stats.get("ready"):
                    break
                time.sleep(0.5)
                stats = get_stats()
            b = briefing.compose(stats)
            _briefing.update(id=_briefing["id"] + 1, at=time.time(), title=b["title"], lines=b["lines"])
            _event("\u203a Briefing delivered")
            return {"ok": True, "reply": b["speech"], "did": ["briefing"]}
        elif re.search(r"\bweather\b|\btemperature\b|\bhow (hot|cold|warm) is it\b|\bwill it rain\b|\bis it (going to )?rain", t):
            reply, did = briefing.weather_line(briefing.weather()), []
        elif _wants_brain(raw, t):
            return _ask_brain(raw, source)
        elif re.search(r"\b(turn|switch|power|shut) (off|down) (the |my )?xbox\b|\b(turn|switch|power) (the |my )?xbox off\b", t):
            if not _cloud():
                return {"ok": False, "reply": "I can't turn the Xbox off until it's linked. Open localhost slash setup slash xbox on the Mac."}
            xboxcloud.turn_off()
            reply, did = "Turning the Xbox off. The TV will follow.", ["xbox_off"]
        elif _cloud() and (cloud_result := _xbox_cloud_command(t)):
            reply, did = cloud_result
        elif re.search(r"\bis (the |my )?xbox (on|awake|running)\b|\bxbox status\b", t):
            reply, did = ("The Xbox is on." if xbox.status(max_age=0) else "The Xbox is asleep or off."), []
        elif re.search(r"\b(turn|switch|power) on (the |my )?xbox\b|\b(turn|switch|power) (the |my )?xbox on\b|\bwake (up )?(the |my )?xbox\b", t):
            if xbox.status(max_age=0):
                reply, did = "The Xbox is already on.", []
            else:
                _xbox_wake()
                _confirm_xbox_wake()
                reply, did = "Waking the Xbox.", ["xbox_on"]
        # matched on the raw text: the clean-up above strips "the tv"
        elif re.search(r"\b(turn|switch|power) on (the )?(tv|television)\b|\b(turn|switch|power) (the )?(tv|television) on\b", raw.lower()):
            if tvcontrol.find():
                reply, did = "The TV is already on.", []
            else:
                _xbox_wake()
                _confirm_xbox_wake()
                reply, did = ("I can't switch the TV on directly, so I've woken the Xbox. "
                              "With HDMI-CEC it should bring the TV up with it."), ["xbox_on"]
        elif re.search(r"\bxbox\b|\blet'?s play\b|\bgam(e|ing) time\b|\bplay (some )?games\b", t):
            woke = False
            if not xbox.status(max_age=0):
                _xbox_wake()
                _confirm_xbox_wake()
                woke = True
            if tvcontrol.find():
                did = (["xbox_on"] if woke else []) + _switch_input(f"HDMI{XBOX_HDMI}", f"KEY_HDMI{XBOX_HDMI}")
                reply = "Waking the Xbox and switching to it." if woke else "Switching to the Xbox."
            else:
                # TV is off (the Xbox switches it off with itself); waking the Xbox brings it back over CEC
                did = ["xbox_on"] if woke else []
                reply = ("Waking the Xbox. It should bring the TV up with it." if woke else
                         "The Xbox is on but the TV is off. Use the TV remote to switch it on.")
        elif re.search(r"\b(stop|end|quit) (casting|mirroring|sharing|streaming)\b|\bstop (the )?(screen|cast)\b", t):
            tvcontrol.cast_stop()
            reply, did = "Screen sharing stopped.", ["cast_stop"]
        elif re.search(r"\b(cast|mirror|share|stream|show)\b.*\b(screen|mac|laptop|computer)\b|\bcast\b", t):
            tvcontrol.cast_start()
            reply, did = "Putting your Mac's screen on the TV. Give it a few seconds.", ["cast_start"]
        elif re.search(r"\b(photo|picture|wallpaper|image)\b", t):
            _photo(server)
            reply, did = "Here's a photo for you.", ["photo"]
        elif re.search(r"\bunmute\b|\bmute\b|\bsilence\b|\bquiet\b", t):
            did = _keys(["KEY_MUTE"])
            reply = "Sound toggled."
        elif re.search(r"\bwhat('?s| is) the volume\b|\bhow loud\b|\bcurrent volume\b", t):
            reply, did = f"The volume is at {tvcontrol.get_volume()}.", []
        elif re.search(r"\bvolume\b", t) and re.search(r"\b(to|at)\s+\d+|\d+\s*(%|percent)|\bset\b|^volume \d+$", t):
            level = tvcontrol.set_volume(_count(t, 20))
            reply, did = f"Volume set to {level}.", [f"volume={level}"]
        elif re.search(r"\b(volume|louder|quieter|softer|sound)\b|\bturn (it )?(up|down)\b", t):
            n = max(1, min(20, _count(t, 5)))  # a mishearing can't blast the TV
            down = bool(re.search(r"\b(down|lower|quieter|softer|decrease|reduce)\b", t))
            try:
                level = tvcontrol.set_volume(tvcontrol.get_volume() + (-n if down else n))
                reply, did = f"Volume {'down' if down else 'up'} {n}, now {level}.", [f"volume={level}"]
            except Exception:
                did = _keys(["KEY_VOLDOWN" if down else "KEY_VOLUP"] * min(n, 10))
                reply = f"Volume {'down' if down else 'up'} {min(n, 10)}."
        elif re.search(r"\b(turn|switch|power) (it |yourself )?off\b|\bshut ?down\b|\bgo to sleep\b|\bgood ?night\b", t):
            did = _keys(["KEY_POWEROFF"])
            reply = "Turning the TV off. Good night."
        elif re.search(r"\bhdmi\b", t):
            n = _count(t, 1)
            did = _switch_input(f"HDMI{n}", f"KEY_HDMI{n}")
            reply = f"Switching to HDMI {n}."
        elif re.search(r"\b(source|input)s?\b", t):
            did = _keys(["KEY_SOURCE"])
            reply = "Here are your inputs."
        elif re.search(r"\bchannel\b", t):
            m = re.search(r"\bchannel (\d{1,4})\b", t)
            if m:
                did = _keys([f"KEY_{d}" for d in m.group(1)] + ["KEY_ENTER"])
                reply = f"Channel {m.group(1)}."
            else:
                down = re.search(r"\b(down|previous|back)\b", t)
                did = _keys(["KEY_CHDOWN" if down else "KEY_CHUP"])
                reply = f"Channel {'down' if down else 'up'}."
        elif re.search(r"\blounge\b|\bhome ?screen\b|\bjarvis app\b", t):
            did = _launch_lounge()
            reply = "Opening Lounge. Give it a few seconds."
        elif short and re.search(r"\b(smart ?hub|apps|home)\b", t):
            did = _keys(["KEY_CONTENTS"])
            reply = "Opening Smart Hub."
        elif re.search(r"\b(live tv|watch tv|antenna|television)\b|^tv$", t):
            did = _keys(["KEY_TV"])
            reply = "Back to live TV."
        elif short and re.search(r"\bexit\b|\bclose\b", t):
            did = _keys(["KEY_EXIT"])
            reply = "Closing."
        elif short and re.search(r"\b(go )?back\b|\breturn\b", t):
            did = _keys(["KEY_RETURN"])
            reply = "Going back."
        elif short and re.search(r"\b(pause)\b", t):
            did = _keys(["KEY_PAUSE"])
            reply = "Paused."
        elif short and re.search(r"\b(play|resume)\b", t):
            did = _keys(["KEY_PLAY"])
            reply = "Playing."
        elif short and re.search(r"\bstop\b", t):
            did = _keys(["KEY_STOP"])
            reply = "Stopped."
        elif short and re.search(r"\b(menu|settings)\b", t):
            did = _keys(["KEY_MENU"])
            reply = "Opening the TV menu."
        elif short and re.search(r"\b(up|down|left|right|ok|okay|select|enter)\b", t):
            word = re.search(r"\b(up|down|left|right|ok|okay|select|enter)\b", t).group(1)
            key = {"ok": "ENTER", "okay": "ENTER", "select": "ENTER", "enter": "ENTER"}.get(word, word.upper())
            did = _keys([f"KEY_{key}"] * max(1, min(10, _count(t, 1))))
            reply = "Done."
        elif short and re.search(r"\b(status|how are you|are you there|you there)\b", t):
            ip = tvcontrol.find()
            reply = (f"All systems online. The TV is at {ip}." if ip else
                     "I can't reach the TV right now. It may be switched off.")
            did = []
        else:
            return _ask_brain(raw, source)  # not a home command: let the brain have a go
    except Exception as e:  # TV off, pairing refused, network hiccup
        return {"ok": False, "reply": f"I couldn't do that: {e}."}

    _event(f"› {reply}")
    if did:  # an action, not a question: answer like a butler
        opener = random.choice(["Right away", "Of course", "Certainly", "Done", "As you wish"])
        if random.random() < 0.4:  # use the name now and then, not every time
            opener += f", {USER_NAME}"
        reply = opener + ". " + reply
    return {"ok": True, "reply": reply, "did": did}


if __name__ == "__main__":
    import sys
    print(handle(" ".join(sys.argv[1:]), server="http://" + (tvcontrol.my_ip() or "127.0.0.1")))
