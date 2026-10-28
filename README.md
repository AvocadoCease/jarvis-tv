# JARVIS for your TV

A home JARVIS that runs on your Mac and talks to your TV. Ask it things out loud, from your phone,
your Mac or the TV remote:
- "volume to 20"
- "switch to the Xbox"
- "what's on my calendar tomorrow?"
- "any emails from Sam?"
- "book a call with Sam at 10, half an hour"

It answers in a calm British voice. The answer also shows on the TV, on a JARVIS-style home screen.

It was built in a day, with Claude Code, for a 2012 Samsung TV and an Xbox. The idea of this repo
is that **your own AI agent adapts it to your TV**. `AGENTS.md` tells it how, including what's
different on older and newer TVs.

## What it does

- **Instant TV commands:** volume (exact), inputs, opening apps, photos, casting your Mac's
  screen, and waking the Xbox. These are rule-based, so they're instant and work offline.
- **Questions and tasks go to Claude:**
  - It reads your calendar, email, Slack and Jira.
  - It books, sends and replies, but **only after you say "go ahead"**. A bare "yes" isn't enough,
    because speech-to-text sometimes hears "No." as "Yes."
  - It uses your own Claude Code login and the connectors you've already set up, so there are no
    API keys to manage.
- **Three ways to talk to it:**
  - **Phone remote:** open `http://<your-mac>/remote` and tap the reactor to speak.
  - **Mac app:** hold **Right Option** to ask JARVIS. Hold or double-tap **fn** to dictate into
    any app; speech-to-text is local Whisper, which also replaces paid dictation apps.
  - **The TV itself:** a JARVIS home screen with live stats, answer cards and a morning-style
    briefing (time, weather, devices).
- **It speaks up:** meeting reminders five minutes before, and (if you use
  T3 Code) when a coding session finishes or needs you. Say "do not disturb"
  to stop it.
- **Privacy mode:** say "guests are here" and it stops putting email and message contents on the
  TV.

## What you need

- **A Mac** (Apple Silicon recommended) running macOS 13 or later, with
  [Homebrew](https://brew.sh).
- **[Claude Code](https://claude.com/claude-code)** installed and logged in, plus any claude.ai
  connectors you want: Google Calendar, Gmail, Slack, Jira…
- **A TV on the same network.** The tested path is a **2011–2013 Samsung Smart TV**. For anything
  else (newer Samsung, LG, Android/Google TV, Roku, or any TV with a Raspberry Pi on HDMI), see
  [`docs/tv-guide.md`](docs/tv-guide.md). Your agent will need to write a small control module
  for it.

## Set it up (the easy way)

Clone this repo, open it in Claude Code (or any coding agent), and say:

> Read AGENTS.md and set JARVIS up for my TV. It's a <brand and model number>.

It will ask you for the few things it can't find itself (your TV's model and network address,
your name and city). It will tell you when you need to press Allow on the TV or tick a macOS
permission.

## Set it up (by hand)

```bash
git clone https://github.com/AvocadoCease/jarvis-tv.git && cd jarvis-tv
cp config.example.json config.json      # your name, city, timezone, TV MAC + model
./install.sh                            # services, local Whisper, the Mac app
./jarvis status
```

Then:
- **Phone remote:** open `http://<your-mac-ip>/remote` on your phone.
- **Mac:** hold Right Option and ask something.
- **Samsung TV home screen:** run `.venv/bin/python tools/build.py --host <your-mac-ip>`, then
  follow "Samsung 2011–2013 TV app" in [`AGENTS.md`](AGENTS.md).

## How it fits together

```
 phone /remote ─┐                    ┌─ tvcontrol.py ──► TV (keys, DLNA volume/photos, inputs)
 Mac app (⌥) ───┼─► serve.py :80 ─► jarvis.py (rules) ─┼─ xbox*.py ─────► Xbox (wake, apps)
 TV app ────────┘        │                             └─ brain.py ─────► claude -p (your login)
                         ├─ voice.py (say → mp3)                            ├ Calendar, Gmail, Slack, Jira
                         ├─ announce.py (reminders)                         └ workmcp.py (T3 sessions)
                         └─ /stats, /api/* for the TV app
```

| Path | What it is |
|---|---|
| `tools/serve.py` | The server: phone remote, TV app files, `/api/command`, `/api/tts` |
| `tools/jarvis.py` | Rule-based commands; everything else goes to the brain |
| `tools/brain.py` | Warm headless Claude Code worker, spoken confirmations, privacy mode |
| `tools/tvcontrol.py` | Samsung 2011–2013 control; **replace this for other TVs** |
| `widget/` | The Samsung Smart Hub (Orsay) TV app; it also runs in a browser at `/preview/` |
| `mac-app/` | Jarvis.app: dictation plus "ask JARVIS", in one Swift file |
| `config.json` | Your settings (git-ignored) |

## Honest limitations

- **macOS only** for the server and the app.
- **The TV-side code is for one TV generation.** Other TVs need a new backend; `AGENTS.md` and
  `docs/tv-guide.md` explain how.
- **Answers from Claude take about 2–6 seconds;** TV commands are instant. Usage counts against
  your Claude plan.
- **The voice is macOS `say`** with a premium voice if you've downloaded one: System Settings →
  Accessibility → Spoken Content → Manage Voices, for example "Jamie (Premium)". It's clear but a
  little robotic.

## Credits and licences

- **Code:** MIT (see `LICENSE`).
- **Fonts:** Manrope and Share Tech Mono, under the SIL Open Font License (text in
  `widget/fonts/`).
- **Wallpapers:** from Unsplash, photographers credited in `widget/js/main.js`.
- **The JARVIS backdrop** is generated by `tools/make_jarvis_assets.py`. Use your own artwork if
  you have rights to it. JARVIS and Iron Man belong to Marvel; this is a fan project.
