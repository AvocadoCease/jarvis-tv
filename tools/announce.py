"""JARVIS speaks up: T3 sessions that finish, fail or need you, and meetings about to start.

Watches T3 Code's database (read-only, via workmcp) every few seconds, and the calendar
(fetched through brain.lookup_json every 90 minutes, cached in ~/.config/lounge/calendar.json).
Announcements queue up for the Mac: Jarvis.app polls /api/announce, shows each one and says it.

Stays quiet when announcements are off ("do not disturb"), between 23:00 and 07:00, and about
finished sessions while you're looking at T3. In a meeting, news is shown but not spoken.

The session JARVIS is following (follow(), see workmcp's follow_session) is different: any finished run,
failure or question is told with a spoken summary of the agent's reply, at any hour and even while you're
in T3, and the Mac listens for your answer afterwards ("reply": True).

  python3 tools/announce.py   prints what it sees now (sessions, calendar), for checking by hand
"""
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

import brain
import settings
import workmcp

POLL_SECONDS = 6
MIN_RUN_SECONDS = 60         # quick replies aren't news
REMIND_MINUTES = 5
CAL_FILE = os.path.expanduser("~/.config/lounge/calendar.json")
CAL_REFRESH_SECONDS = 90 * 60
T3_BUNDLE = "com.t3tools.t3code"
T = workmcp.T

_items = []
_next_id = [1]
_lock = threading.Lock()
_cal = {"events": [], "fetched": 0}
_reminded = set()
_refreshing = threading.Lock()


def enabled():
    return bool(brain.setting("announce", True))


def _quiet_hours():
    hour = time.localtime().tm_hour
    return hour >= 23 or hour < 7


def _push(text, spoken=True, reply=False):
    with _lock:
        _items.append({"id": _next_id[0], "text": text, "speak": spoken, "reply": reply, "at": time.time()})
        _next_id[0] += 1
        del _items[:-20]
    brain.note_announcement(text)
    print(f"ANNOUNCE {'say' if spoken else 'show'}: {text}", flush=True)


def since(after):
    """New announcements for the Mac. after=None just returns the latest id (nothing replays on launch)."""
    with _lock:
        latest = _next_id[0] - 1
        items = [] if after is None else [i for i in _items if i["id"] > after and time.time() - i["at"] < 180]
    return {"latest": latest, "items": items, "enabled": enabled()}


def _looking_at_t3():
    """T3 is the front app and the keyboard or mouse was used in the last two minutes."""
    try:
        front = subprocess.run(["lsappinfo", "info", "-only", "bundleid", subprocess.run(
            ["lsappinfo", "front"], capture_output=True, text=True, timeout=3).stdout.strip()],
            capture_output=True, text=True, timeout=3).stdout
        idle = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=3).stdout
        idle_s = int(re.search(r'"HIDIdleTime" = (\d+)', idle).group(1)) / 1e9
        return T3_BUNDLE in front and idle_s < 120
    except Exception:
        return False


# ---- T3 sessions ------------------------------------------------------------------------------

def _snapshot():
    since_iso = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
    snap = {}
    with workmcp._db() as con:
        for t in workmcp._top_level_threads(con, since_iso)[:60]:
            run = con.execute(f"select run_id, status, requested_at, completed_at from {T}runs where thread_id = ? "
                              "order by ordinal desc limit 1", (t["thread_id"],)).fetchone()
            pending = con.execute(f"select runtime_request_id, kind from {T}runtime_requests "
                                  "where thread_id = ? and status = 'pending'", (t["thread_id"],)).fetchall()
            snap[t["thread_id"]] = {"title": t["title"], "run": run and run["run_id"],
                                    "status": run and run["status"], "asked": run and run["requested_at"],
                                    "done": run and run["completed_at"],
                                    "pending": {r["runtime_request_id"]: r["kind"] for r in pending}}
    return snap


def _seconds(a, b):
    try:
        parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (parse(b) - parse(a)).total_seconds()
    except (TypeError, ValueError, AttributeError):
        return 0


def _name(title):
    """A session's title as you'd say it: short, no trailing punctuation."""
    words = re.sub(r"[\"“”]", "", title).split()
    return " ".join(words[:7]).rstrip(".:,;")


def _join(names):
    names = [f"{n}" for n in names]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _changes(old, new, followed=None):
    needs, finished, failed, mine = [], [], [], []
    for tid, s in new.items():
        before = old.get(tid) or {"run": None, "status": None, "pending": {}}
        changed = s["run"] != before["run"] or s["status"] != before["status"]
        if tid == followed:   # told separately, in full (see _tell_followed)
            if set(s["pending"]) - set(before["pending"]):
                mine.append(("needs", tid, s))
            elif changed and s["status"] in ("completed", "failed"):
                mine.append((s["status"], tid, s))
            continue
        if set(s["pending"]) - set(before["pending"]):
            needs.append((s["title"], set(s["pending"].values())))
        if changed and s["status"] in ("completed", "failed") and _seconds(s["asked"], s["done"]) >= MIN_RUN_SECONDS:
            (finished if s["status"] == "completed" else failed).append(s["title"])
    return needs, finished, failed, mine


# ---- the session JARVIS is following ----------------------------------------------------------

def following():
    f = brain.setting("following")
    return f if isinstance(f, dict) and f.get("thread") else None


def follow(thread, title):
    brain.set_setting("following", {"thread": thread, "title": title, "since": time.time()})
    print(f"ANNOUNCE following: {title}", flush=True)


def unfollow():
    was = following()
    brain.set_setting("following", None)
    print(f"ANNOUNCE stopped following: {was and was['title']}", flush=True)
    return was and was["title"]


def _tell_followed(kind, tid, s):
    """The followed session finished, failed or needs the user: say so, with a summary of what it said."""
    if not enabled():
        return
    with workmcp._db() as con:
        reply = workmcp._last_message(con, tid, "assistant", s["run"])
    summary = brain.summarise(reply) if reply else ""
    name = _name(s["title"])
    if kind == "needs":
        kinds = set(s["pending"].values())
        what = "has a question for you" if kinds & {"user_input", "question"} else "needs your approval in T3"
        text = f"{brain.USER_NAME}, the {name} session {what}."
    elif kind == "failed":
        text = f"The {name} session failed."
    else:
        text = f"The {name} session is done."
    _push(f"{text} {summary}".strip(), spoken=not _in_meeting(), reply=True)


def follow_context():
    """For the brain: which session "it" is."""
    f = following()
    if not f:
        return ""
    try:
        with workmcp._db() as con:
            _, status = workmcp._state(con, f["thread"])
            last = workmcp._last_message(con, f["thread"], "assistant")
    except Exception:
        status, last = "unknown", ""
    return (f"You're following the T3 session \u201c{f['title']}\u201d ({status}). Unless {brain.USER_NAME} names "
            "another session, \"it\", \"the session\" or \"tell it...\" mean this one (use its exact title with "
            "message_session)." + (f" Its latest message ends: \u201c{workmcp._clip(last, 500, keep_end=True)}\u201d"
                                   if last else ""))


def _announce_sessions(needs, finished, failed, mine=()):
    for kind, tid, s in mine:   # the summary takes a few seconds: don't hold up the watcher
        threading.Thread(target=_tell_followed, args=(kind, tid, s), daemon=True).start()
    if not (needs or finished or failed) or not enabled() or _quiet_hours():
        return
    if _looking_at_t3():
        finished, failed = [], []        # you can see those already
    lines = []
    for title, kinds in needs:
        what = "has a question for you" if kinds & {"user_input", "question"} else "needs your approval"
        lines.append(f"{brain.USER_NAME}, the {_name(title)} session {what}.")
    if failed:
        lines.append(f"The {_join([_name(t) for t in failed])} session{'s have' if len(failed) > 1 else ' has'} failed.")
    if finished:
        lines.append(f"The {_join([_name(t) for t in finished])} session{'s have' if len(finished) > 1 else ' has'} finished.")
    if lines:
        _push(" ".join(lines), spoken=not _in_meeting())


# ---- calendar -----------------------------------------------------------------------------------

CAL_PROMPT = """Use Google Calendar list_events on the primary calendar from {start} to {end} ({tz} time).
Reply with only a JSON array, one object per event, in time order:
{{"title": "...", "start": "ISO 8601 with UTC offset", "end": "ISO 8601 with UTC offset", "all_day": false,
"declined": false, "meet": false, "with": ["Sam Lee"]}}
declined is true if the calendar owner declined it; meet is true if it has a video call link; with lists the other
attendees' names (or emails if no name), at most 6, not the owner. [] if there are none."""


def _load_calendar():
    try:
        with open(CAL_FILE) as f:
            _cal.update(json.load(f))
    except (OSError, ValueError):
        pass


def refresh_calendar(*_):
    if not _refreshing.acquire(blocking=False):
        return
    try:
        now = datetime.now()
        data = brain.lookup_json(CAL_PROMPT.format(start=now.strftime("%Y-%m-%dT%H:%M"),
                                                   end=(now + timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M"),
                                                   tz=settings.TIMEZONE))
        if isinstance(data, list):
            _cal.update(events=data, fetched=time.time(), until=time.time() + 36 * 3600)
            os.makedirs(os.path.dirname(CAL_FILE), exist_ok=True)
            with open(CAL_FILE, "w") as f:
                json.dump(_cal, f)
            print(f"ANNOUNCE calendar: {len(data)} events", flush=True)
    finally:
        _refreshing.release()


def _meetings():
    for e in _cal["events"]:
        if e.get("all_day") or e.get("declined"):
            continue
        try:
            start = datetime.fromisoformat(e["start"].replace("Z", "+00:00")).astimezone()
            end = datetime.fromisoformat(e["end"].replace("Z", "+00:00")).astimezone()
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        yield e, start, end


def _in_meeting():
    now = datetime.now().astimezone()
    return any(start <= now < end for _, start, end in _meetings())


def _remind():
    now = datetime.now().astimezone()
    for e, start, _ in _meetings():
        minutes = (start - now).total_seconds() / 60
        key = f"{e.get('title')}|{e['start']}"
        if key in _reminded or not 0 < minutes <= REMIND_MINUTES + 0.25:
            continue
        _reminded.add(key)
        if not enabled() or _quiet_hours():
            continue
        m = round(minutes)
        when = "is starting now" if m < 1 else "starts in a minute" if m == 1 else f"starts in {m} minutes"
        title = " ".join((e.get("title") or "your meeting").split())
        _push(f"{brain.USER_NAME}, {title} {when}."
              + (" It has a Meet link." if e.get("meet") else ""), spoken=not _in_meeting())


def calendar_context():
    """The cached calendar, for the brain: lets it answer "what's on tomorrow?" without a lookup."""
    if not _cal["fetched"] or time.time() - _cal["fetched"] > 6 * 3600:
        return ""
    until = datetime.fromtimestamp(_cal.get("until") or _cal["fetched"] + 36 * 3600)
    rows = []
    for e in _cal["events"]:
        try:
            start = datetime.fromisoformat(e["start"].replace("Z", "+00:00")).astimezone()
            end = datetime.fromisoformat(e["end"].replace("Z", "+00:00")).astimezone()
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if end < datetime.now().astimezone():
            continue
        when = f"{start:%a %d %b} all day" if e.get("all_day") else f"{start:%a %d %b %H:%M}-{end:%H:%M}"
        extra = (f" with {', '.join(e['with'])}" if e.get("with") else "") + (" (Meet)" if e.get("meet") else "") \
            + (" (declined)" if e.get("declined") else "")
        rows.append(f"{when} {' '.join(str(e.get('title')).split())}{extra}")
    minutes = round((time.time() - _cal["fetched"]) / 60)
    return (f"Primary calendar from {minutes} min ago, up to {until:%a %H:%M}: " + ("; ".join(rows) or "nothing")
            + ". Answer questions within that window from this list without looking anything up; use the "
              "calendar tools for later dates, other calendars, event details, or if asked to check again.")


def status():
    upcoming = [f"{e.get('title')} at {start:%a %H:%M}" for e, start, _ in _meetings()
                if start > datetime.now().astimezone()][:5]
    return {"enabled": enabled(), "calendar_fetched": int(_cal["fetched"]), "upcoming": upcoming,
            "in_meeting": _in_meeting()}


def start():
    """Run the watchers in the background. Call once at server start."""
    _load_calendar()
    brain.context.append(calendar_context)
    brain.context.append(follow_context)
    brain.after_action.append(lambda tool: "Google_Calendar" in tool and refresh_calendar())

    def sessions():
        prev = None
        while True:
            try:
                snap = _snapshot()
                if prev is not None:
                    f = following()
                    _announce_sessions(*_changes(prev, snap, f and f["thread"]))
                prev = snap
            except Exception as e:
                print("ANNOUNCE sessions error:", repr(e)[:200], flush=True)
            time.sleep(POLL_SECONDS)

    def calendar():
        time.sleep(30)  # let the brain warm up first
        while True:
            try:
                hour = time.localtime().tm_hour
                if time.time() - _cal["fetched"] > CAL_REFRESH_SECONDS and 6 <= hour < 22:
                    refresh_calendar()
                _remind()
            except Exception as e:
                print("ANNOUNCE calendar error:", repr(e)[:200], flush=True)
            time.sleep(20)

    threading.Thread(target=sessions, daemon=True).start()
    threading.Thread(target=calendar, daemon=True).start()


if __name__ == "__main__":
    snap = _snapshot()
    for s in snap.values():
        print(f"{s['status']:<12} {len(s['pending'])} pending  {_name(s['title'])}")
    print("looking at T3:", _looking_at_t3(), " quiet hours:", _quiet_hours())
    _load_calendar()
    print(json.dumps(status(), indent=1))
