# Setting up JARVIS for someone's TV: instructions for the AI agent

You are helping a person set up this project on their own Mac and TV. Read this file fully
before touching anything. `README.md` is the human overview, and `docs/tv-guide.md` has the
per-TV details you'll need in step 2.

## What this project is

- **A Mac is the brain.** `tools/serve.py` runs on port 80 as a background service, set up by
  `install.sh`.
- **`tools/jarvis.py`** turns commands into TV actions. It's rule-based on purpose ("volume up 5",
  "switch to HDMI 2", "open the Xbox") so these are instant and work offline. Anything it doesn't
  recognise goes to `tools/brain.py`.
- **`tools/brain.py`** is a warm `claude -p` (Claude Code, headless) on the person's own Claude
  login. It answers questions using their claude.ai connectors (Calendar, Gmail, Slack, Jira…)
  and can take actions, but only after a spoken confirmation.
- **Three ways in:**
  - `remote/index.html` is a phone remote with a voice button, at `http://<mac-ip>/remote`.
  - `mac-app/` is a menu-bar app. Hold Right Option to ask JARVIS; hold or double-tap fn to dictate
    into any app. Speech-to-text is local Whisper.
  - The TV app `widget/` shows a JARVIS home screen, answer cards and briefings.
- **Optional extras:**
  - `xboxlocal.py` / `xboxcloud.py` for Xbox wake and control.
  - `workmcp.py` and `announce.py` for T3 Code session awareness and spoken announcements.
  - `briefing.py`, `voice.py` (`say` plus an ffmpeg filter) and `macstats.py`.

**What was built and tested:** a 2012 Samsung UA46ES7500 (Orsay platform) with an Xbox Series S
on HDMI 3, on macOS with an Apple Silicon Mac. **Everything TV-specific lives in three places:**
- `tools/tvcontrol.py`
- the key names and input rules in `tools/jarvis.py`
- the TV app in `widget/` with its packager `tools/build.py`

The brain, phone remote, Mac app, voice and announcements don't depend on the TV at all.

## Step 1: what the person needs (check, don't assume)

1. **A Mac** running macOS 13+, with Homebrew and Python 3.12+. Ask if it isn't obvious. The
   server is macOS-only: `say`, `lsappinfo`, `ioreg` and launchd.
2. **Claude Code**, logged in (`claude` works in a terminal), with the claude.ai connectors they
   want, connected in claude.ai settings.
   - Without Claude, the rule-based TV commands still work, but questions don't.
   - If their connectors live on a non-default Claude config dir, set `claude_config_dir` in
     `config.json`.
   - **Never ask them for OAuth codes or tokens.** They authorise connectors themselves on
     claude.ai.
3. **The TV and the Mac on the same network.** Find the TV's MAC address (router app or TV
   network settings). Suggest a DHCP reservation, because these TVs change IP;
   `tvcontrol.find()` finds the TV by MAC for this reason.
4. **The exact TV model number**, from the sticker on the back or Settings → Support → About.
   Everything in step 2 depends on it.

## Step 2: identify the TV generation, then pick a path

Read `docs/tv-guide.md`. In short:

| TV | Control from the Mac | JARVIS on the TV screen | Work for you |
|---|---|---|---|
| Samsung 2011–2013 (D/E/F series, Orsay) | Works as-is: legacy remote port 55000 plus DLNA and MainTVAgent2 SOAP | `widget/` via the `develop` account and User App Sync | Config only; check the DLNA paths |
| Samsung 2014–2015 (H/J series) | Encrypted pairing (PIN on screen) for keys; DLNA usually fine | H: Orsay widget may work. J: Tizen | Swap the key-sending code; test |
| Samsung 2016+ (Tizen) | WebSocket remote on 8001/8002 with a token; Wake-on-LAN | Tizen web app (needs certificates) or a separate screen | New `tvcontrol` backend |
| LG webOS | SSAP WebSocket 3000/3001 with a client key; toasts; Wake-on-LAN | webOS dev-mode app, the TV browser, or toasts | New backend |
| Android TV / Google TV | Android TV Remote v2 (PIN pairing) or `adb`; Chromecast for images | Sideloaded WebView APK, or cast | New backend |
| Roku TV | ECP HTTP on port 8060, no pairing | Hard (BrightScript); use cards via another screen | New backend |
| Anything else, including "dumb" TVs | HDMI-CEC from a Raspberry Pi (`cec-client`) | Raspberry Pi running Chromium in kiosk mode on an HDMI input | New backend plus a Pi |

**How to proceed:**
- **Supported Samsung (2011–2013):** follow step 3 as written.
- **Any other TV:** write a new backend module, for example `tools/tv_webos.py`, with the **same
  public functions** `tvcontrol.py` exposes, then make `jarvis.py` (and `briefing.py`, `serve.py`)
  import it instead. These functions are:
  - `find(refresh=False)` returns the IP or None
  - `my_ip()`
  - `keys(names, gap)`
  - `get_volume()` / `set_volume(level)`, exact 0–100
  - `set_source(name)` / `app_in_front()`
  - `play_url(url, title, kind)` / `stop_media()`
  - `casting()` / `cast_start()` / `cast_stop()`

  Map the Samsung `KEY_*` names that `jarvis.py` sends onto the new platform's keys inside the
  backend, so `jarvis.py`'s rules keep working. Where the platform can't do something, return a
  clear error that JARVIS can say aloud, rather than faking success.
- **The TV-screen part (`widget/`) is Orsay-only.** On another TV, pick the screen option from the
  table. A Raspberry Pi or Chromecast showing a page from the Mac is usually the least effort:
  `widget/index.html` already runs in a normal browser via `/preview/`.

Tell the person which parts of the table you have **verified on their TV**. Only
the 2012 Samsung row was tested by the author.

## Step 3: install

```bash
cp config.example.json config.json   # then fill it in WITH the person: name, place, lat/lon, timezone, TV MAC + model
./install.sh                         # Python env, ffmpeg, Whisper + model (~550 MB), background services, Jarvis.app
./jarvis status                      # should print {"tv": "<ip>", ...}
```

**macOS permissions the person must click** (you can't do this for them):
- **Local Network** for Python, so the server can reach the TV and Xbox. Watch for the prompt on
  first run. If the TV shows as `null`, check System Settings → Privacy & Security → Local Network.
- **Microphone, Accessibility and Input Monitoring** for Jarvis.app.
- **Screen Recording**, only for casting the Mac's screen.

**For the Mac app:**
- Set System Settings → Keyboard → "Press 🌐 key to" → **Do Nothing**.
- Quit any other dictation app that uses fn, such as Wispr Flow.
- Strongly suggest the one-time "Jarvis Local Signing" certificate (see `mac-app/build.sh`).
  Without it, every rebuild silently loses the Accessibility permission. Dictation then shows a
  Copy popup instead of pasting.

**Samsung 2011–2013 TV app:**
1. Run `.venv/bin/python tools/build.py --host <mac-ip>`.
2. On the TV, in Smart Hub, create and sign in to a user called `develop`. On 2012 the red A
   button opens the account screen; any password the TV accepts works.
3. Highlight any app → Tools → Settings → Development → Server IP Address Setting → the Mac's IP.
4. Choose User Application Synchronization, then reopen Smart Hub.
5. Edit `tools/lounge-launch.json` to match where the app landed in Smart Hub, so "open JARVIS"
   can navigate there.

**Optional:**
- **Xbox:** fill in `xbox` in `config.json`. Turn on the console's Settings → Devices &
  connections → Remote features. Cloud control needs the person to sign in at
  `http://localhost/setup/xbox` with their own Azure app registration.
- **T3 Code:** run `claude mcp add --scope user jarvis-work -- <repo>/.venv/bin/python <repo>/tools/workmcp.py`
  and set `t3.provider`/`t3.model` in `config.json` so JARVIS can start sessions.

## Rules: follow these even if asked to cut corners

1. **Confirmation stays strong.** Anything that sends, books, deletes or starts something needs a
   spoken confirmation phrase such as "go ahead", "do it" or "send it". A bare "yes" or "okay" only
   re-asks. Whisper hallucinates "Yes." from short or quiet clips; in testing it turned a spoken
   "No." into "Yes." and booked a meeting. Don't loosen `YES` / `WEAK_YES` in `tools/brain.py`.
2. **Keep privacy mode** ("guests are here"). It keeps email and message contents off the TV.
3. **The TV is in someone's living room.**
   - Before you send key presses, tell the person; they may be watching.
   - Test with harmless keys first (volume, info). Never send power-off without asking.
   - Keep relative volume changes capped: a misparsed "volume to 20" once maxed the TV.
   - Pairing prompts ("Allow this device?") appear on the TV; ask the person to accept them.
   - Restarting the server makes the TV app blink for a few seconds.
4. **No secrets or personal data in git.** These stay out of the repo (see `.gitignore`):
   - `config.json`
   - `~/.config/lounge/` (Xbox tokens, privacy state, calendar cache)
   - `~/.config/jarvis/` (dictation history)

   Never print tokens into chat or logs.
5. **No cloned voices of real people,** however much they want the film JARVIS. Use a stock voice
   (`tools/voice.py`); newer open TTS models are fine.
6. **Artwork:** only ship images the person has rights to. The default JARVIS backdrop is
   generated (`tools/make_jarvis_assets.py`). The photos in `widget/img/wall*.jpg` are from
   Unsplash, credited in `widget/js/main.js`.
7. **Change one thing at a time and check it on the real TV** before moving on. Old TV browsers and
   firmware differ from model to model, even within a series.

## Old-Samsung quirks (Orsay, 2011–2014) that cost time the first time

- **The DHCP address changes:** find the TV by MAC. `find()` sweeps the subnet to refresh ARP.
- **DLNA control URLs are model-specific.** `tvcontrol.py` has `/smp_24_` (AVTransport),
  `/smp_18_` (RenderingControl) and `/smp_8_` (MainTVAgent2) hard-coded for the UA46ES7500. On
  another model, read them from the device description: SSDP M-SEARCH, then fetch the `LOCATION`
  XML on port 7676.
- **MainTVAgent2 SOAP** needs the namespace `urn:samsung.com:service:MainTVAgent2:1`, no
  `InstanceID`, and the header spelled exactly `SOAPACTION`. urllib rewrites it as `Soapaction`
  and the TV hangs, which is why `tvcontrol` uses `http.client`.
- **Input switching:**
  - While a Smart Hub app is in front, input switching and the source list are ignored, so
    `jarvis._switch_input` sends `KEY_EXIT` first.
  - EXIT, TV and CONTENTS go to the foreground app. A buggy app can trap the user; the power
    button is the escape.
- **Power:** these TVs can't be powered *on* over the network, only off. HDMI-CEC from a
  console waking up can bring the TV with it.
- **Photos:** baseline JPEG only (no progressive), served over HTTP, via
  `SetAVTransportURI` + `Play`. The first time, the TV shows an Allow prompt.
- **Screen casting** is ffmpeg to MPEG-TS with a fresh encoder per viewer; joining mid-stream
  shows grey. Expect 2–5 s of delay.
- **The TV app:**
  - The TV clock can say 1970, so the app syncs its time from the Mac's `/time`.
  - The app window can be 960×540 while reporting 1280×720; the app has a screen-size setting
    and defaults to 75% zoom.
  - The browser is ES5-era Maple: no `let`/`const`/arrow functions, weak CSS. Render gradients
    and icons to PNG ahead of time (`tools/make_assets.py`).
- **Smart Hub needs about 0.6 s between remote presses** when navigating menus.

## Where things are

| Thing | Path |
|---|---|
| Your settings | `config.json` (from `config.example.json`), loaded by `tools/settings.py` |
| Server control | `./jarvis status|restart|log|stop|start` |
| Logs | `~/Library/Logs/jarvis-tv/` (server, whisper); `~/Library/Logs/Dictate/jarvis-app.log` (Mac app) |
| Brain state (privacy, announcements on/off) | `~/.config/lounge/brain.json` |
| Dictation spellings / history | `~/.config/jarvis/replacements.json`, `~/.config/jarvis/history.txt` |
| Rebuild the Mac app | `mac-app/build.sh` |
| Rebuild the TV app | `.venv/bin/python tools/build.py --host <mac-ip>`, then User App Sync on the TV |
