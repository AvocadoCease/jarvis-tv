"""JARVIS's briefing: time, local weather and the state of the house's devices.

compose(stats) -> {"title": "BRIEFING 13:05", "lines": [...], "speech": "..."}

Template-written so it needs no API key. Each line is one short sentence: the
TV types them out one at a time while the Mac speaks the whole thing.
"""
import json
import random
import time
import urllib.request

import settings
import tvcontrol
import xboxlocal as xbox

USER_NAME = settings.USER_NAME

PLACE = settings.PLACE
LAT, LON, TZ = settings.LAT, settings.LON, settings.TIMEZONE

WMO = {
    0: "clear skies", 1: "mostly clear skies", 2: "partly cloudy skies", 3: "overcast skies",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "heavy rain showers", 82: "violent rain showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "severe thunderstorms with hail",
}

_weather = {"data": None, "at": 0.0}


def weather():
    if _weather["data"] and time.time() - _weather["at"] < 900:
        return _weather["data"]
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={LAT}&longitude={LON}&timezone={TZ.replace('/', '%2F')}&forecast_days=1"
           "&current=temperature_2m,apparent_temperature,weather_code"
           "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunset")
    with urllib.request.urlopen(url, timeout=8) as r:
        d = json.load(r)
    c, day = d["current"], d["daily"]
    data = {
        "now": round(c["temperature_2m"]), "feels": round(c["apparent_temperature"]),
        "sky": WMO.get(c["weather_code"], "mixed conditions"),
        "high": round(day["temperature_2m_max"][0]), "low": round(day["temperature_2m_min"][0]),
        "rain": day["precipitation_probability_max"][0] or 0,
        "sunset": _clock(time.strptime(day["sunset"][0], "%Y-%m-%dT%H:%M")),
    }
    _weather.update(data=data, at=time.time())
    return data


def weather_line(w):
    line = f"In {PLACE} it's {w['now']} degrees with {w['sky']}"
    if abs(w["feels"] - w["now"]) >= 3:
        line += f", feeling like {w['feels']}"
    line += f". Expect a high of {w['high']} and a low of {w['low']}."
    if w["rain"] >= 50:
        line += f" There's a {w['rain']} percent chance of rain, so an umbrella wouldn't go amiss."
    elif w["rain"] >= 20:
        line += f" A {w['rain']} percent chance of rain."
    return line


def _ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _clock(t):
    h = t.tm_hour % 12 or 12
    return f"{h}:{t.tm_min:02d} {'AM' if t.tm_hour < 12 else 'PM'}"


def compose(stats=None):
    t = time.localtime()
    part = "morning" if 5 <= t.tm_hour < 12 else "afternoon" if t.tm_hour < 18 else "evening"
    lines = [
        f"Good {part}, {USER_NAME}.",
        f"It's {_clock(t)} on {time.strftime('%A', t)}, the {_ordinal(t.tm_mday)} of {time.strftime('%B', t)}.",
    ]

    try:
        w = weather()
        lines.append(weather_line(w))
        if part != "evening":
            lines.append(f"Sunset is at {w['sunset']}.")
    except Exception:
        lines.append("I couldn't reach the weather service just now.")

    # The house
    try:
        tv = tvcontrol.find()
        if tv:
            try:
                lines.append(f"The TV is on, at volume {tvcontrol.get_volume()}.")
            except Exception:
                lines.append("The TV is on.")
        else:
            lines.append("The TV is off.")
    except Exception:
        pass
    try:
        lines.append("Your Xbox is awake." if xbox.status() else "Your Xbox is asleep.")
    except Exception:
        pass

    s = stats or {}
    bat = s.get("battery")
    mem = s.get("mem") or {}
    if bat:
        state = bat.get("state", "")
        line = f"Your Mac is at {bat['pct']} percent battery"
        line += ", charging." if "charg" in state and "dis" not in state else "."
        if bat["pct"] <= 20 and "dis" in state:
            line += " You may want to plug it in."
        lines.append(line)
    if mem.get("pct") and mem["pct"] >= 85:
        lines.append(f"Memory is running high at {mem['pct']} percent.")

    lines.append(random.choice(["That's everything for now.", "All systems are running smoothly.",
                                "That's your briefing.", "Standing by."]))
    return {"title": "BRIEFING " + time.strftime("%H:%M", t), "lines": lines, "speech": " ".join(lines)}


if __name__ == "__main__":
    b = compose()
    print(b["title"])
    for line in b["lines"]:
        print(" ", line)
