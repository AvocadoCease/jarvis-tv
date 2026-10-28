# Which TV do you have? Controlling it, and getting JARVIS on its screen

Only the **2012 Samsung (Orsay)** path was built and tested end to end. Everything else below is
the best-known way in, from the platforms' public or community-documented APIs. **Verify each
capability on the real TV** before relying on it; firmware differs even within one model year.

JARVIS needs two different things from a TV:

1. **Control:** keys, exact volume, input switching, power, showing a photo or video. That's
   `tools/tvcontrol.py`, or a new backend with the same functions; see `AGENTS.md`, step 2.
2. **A screen:** the home screen, answer cards and briefings. On old Samsungs that's the
   `widget/` app. Elsewhere it's usually easier to use a browser on the TV, a cast, or a cheap
   HDMI device.

## Find the model year

Read the model number off the sticker on the back, or from Settings → Support → About.

**Samsung**, letter after the screen size (e.g. UA46**E**S7500 = 2012):

| Letter | Year |
|---|---|
| D | 2011 |
| E | 2012 |
| F | 2013 |
| H | 2014 |
| J | 2015 |
| K | 2016 |
| M | 2017 |
| N / NU | 2018 |
| R / RU | 2019 |
| T / TU | 2020 |
| A / AU | 2021 |
| B / BU | 2022 |
| C / CU | 2023 |
| D / DU | 2024 |

Newer QLED and OLED names put the letter at the end (QE55Q80**T**). The letters repeat across
decades, so check the year on the sticker if in doubt.

**LG:**
- Pre-2014 "NetCast" sets use the older UDAP protocol.
- webOS sets (2014+) include the year in the name (OLED55**C9** = 2019). Settings → Support shows
  the webOS version.

**Android TV / Google TV:** Sony Bravia, Philips, TCL, Hisense, Nvidia Shield and Chromecast with
Google TV. Look for the Google Play Store on the TV.

**Roku TV:** TCL and Hisense Roku models. The home screen says Roku.

---

## Samsung 2011–2013: D, E, F series (Orsay). Tested on a 2012 ES7500.

**Control: works as-is in `tools/tvcontrol.py`.**
- **Keys:** legacy remote protocol on TCP **55000**. The first connection makes the TV ask
  Allow/Deny; accept it once.
- **Exact volume:** DLNA RenderingControl `SetVolume`/`GetVolume`. `GetVolume` lags a second or
  two behind.
- **Inputs:** `MainTVAgent2` SOAP (`GetSourceList`, `SetMainTVSource`). Ignored while a Smart Hub
  app is in front, so exit the app first.
- **Photos and video:** DLNA AVTransport `SetAVTransportURI` + `Play` of an HTTP URL on the Mac.
  JPEGs must be **baseline**.
- **Screen cast:** ffmpeg to MPEG-TS over HTTP, one encoder per viewer. Expect 2–5 s of delay.
- **Power:** off yes, **on no** (no Wake-on-LAN). HDMI-CEC from a console waking up can turn the
  TV on with it.
- **Control URLs** (`/smp_24_`, `/smp_18_`, `/smp_8_`) and the `iphone.<MODEL>.iapp.samsung`
  app string are per model. Get the right ones from the SSDP description XML on port 7676.

**Screen: `widget/`**, an ES5 Smart Hub app installed by "User App Sync":
1. `tools/build.py --host <mac-ip>` packages it, and `serve.py` on **port 80** serves
   `/widgetlist.xml` plus the zip.
2. On the TV, open Smart Hub and create or sign in to the user **`develop`**.
3. Tools → Settings → Development → Server IP Address Setting → the Mac's IP.
4. Choose User Application Synchronization, then reopen Smart Hub.

Quirks that apply here:
- Apps can't be deleted over the network; use the remote (Smart Hub → Tools → Delete).
- The TV clock may say 1970.
- The app window may be 960×540.
- The browser has no ES6.

## Samsung 2014–2015: H, J series

**Control:**
- Many H and J models require **encrypted pairing**: a PIN on the TV, then an encrypted
  WebSocket on port 8000.
- Some cheaper H models still answer the legacy port 55000; try that first.
- Home Assistant's Samsung integration and its libraries are a good reference for the
  handshake.
- DLNA volume, photos and video usually still work.

**Screen:**
- H (2014) is still Orsay, so `widget/` may install the same way, with different menu names.
- J (2015) is Tizen: treat it like 2016+.

## Samsung 2016+ (Tizen): K, M, N, R, T, A, B, C, D…

**Control:**
- **Keys:** WebSocket remote at `ws://TV:8001/api/v2/channels/samsung.remote.control?name=<base64 name>`.
  2018+ use `wss://TV:8002/…` and return a **token** after the on-screen Allow; save it and send
  it on every connection.
  - Python: `samsungtvws`.
  - Key names are the same `KEY_*` names, so `jarvis.py`'s rules mostly carry over.
- **Device info:** `http://TV:8001/api/v2/`.
- **Launch apps:** `POST http://TV:8001/api/v2/applications/<appId>`.
- **Power on:** Wake-on-LAN, with the TV's "Power On with Mobile" / network standby enabled.
- **Volume:** keys. Exact DLNA volume is model-dependent.
- **Alternative:** SmartThings cloud API, which needs a personal access token that the person
  creates themselves.

**Screen:**
- A Tizen web app needs Tizen Studio, Samsung certificates, and Developer Mode (Apps panel →
  12345 → the Mac's IP). It's doable but fiddly.
- Easier: open the TV's browser on `http://<mac-ip>/preview/`, or use a Chromecast or
  Raspberry Pi.

## LG webOS (2014+)

**Control: SSAP over WebSocket.**
- `ws://TV:3000`; newer firmware needs `wss://TV:3001`.
- The first connection shows a pairing prompt, which returns a **client key**; store it.
- Python: `aiowebostv` (Home Assistant) or `pywebostv`.
- Useful URIs:
  - `ssap://audio/setVolume`: exact volume
  - `ssap://tv/switchInput`
  - `ssap://system.launcher/launch` and `…/open`: open a URL in the browser
  - `ssap://system.notifications/createToast`: a nice fit for JARVIS's answers and
    announcements
  - `ssap://system/turnOff`
  - Button presses go through a separate pointer-input socket.
- **Power on:** Wake-on-LAN, with "Turn on via Wi-Fi" / "Mobile TV On" enabled.

**Screen:** toasts for short answers. Otherwise launch the browser at the Mac's page, or a webOS
Developer Mode app; that needs an LG developer account, and the dev session expires unless
renewed.

**Pre-2014 LG NetCast:** UDAP on port 8080 with a pairing key shown on the TV. Limited.

## Android TV / Google TV (Sony, Philips, TCL, Hisense, Shield, Chromecast with Google TV)

**Control:**
- **Android TV Remote protocol v2:** PIN pairing, ports 6466/6467, no developer mode needed.
  Python: `androidtvremote2`. Gives keys, power and launching apps by link.
- **Or `adb`:** Developer options → network or wireless debugging.
  - Keys: `adb shell input keyevent`.
  - Apps: `adb shell am start`.
  - Volume: `adb shell media volume` or the volume keys.
- **Sony Bravia** also has a REST "IP control" API with a pre-shared key (Settings → Network →
  Home network → IP control).
- **Photos and video:** Chromecast built-in (Python: `pychromecast`).

**Screen:** cast a page or card to the TV, or sideload a tiny WebView APK pointing at
`http://<mac-ip>/preview/`.

## Roku TV (TCL, Hisense Roku models)

**Control: ECP**, plain HTTP on port **8060** with no pairing:
- `POST /keypress/<Key>`
- `POST /launch/<appId>`
- `GET /query/device-info`
- `POST /keypress/PowerOn` turns on Roku TVs, with fast start enabled.

On newer Roku OS, set Settings → System → Advanced → Control by mobile apps → Network access to
allow it.

**Screen:** custom channels are BrightScript via developer mode. Hard; prefer a separate HDMI
screen device.

## Apple TV, Fire TV

- **Apple TV:** `pyatv` (Companion and AirPlay pairing). It handles remote keys, power and
  AirPlay photos and video. A custom app needs Xcode and a developer account.
- **Fire TV:** `adb` on port 5555 (Developer options → ADB debugging), same as Android.

## Any TV, including "dumb" ones: a Raspberry Pi on an HDMI input

- **Screen:** Chromium in kiosk mode on `http://<mac-ip>/preview/` gives the full JARVIS screen.
- **Control:** HDMI-CEC via `cec-client` (libcec): `on 0`, `standby 0`, and `as` to make the Pi
  the active input. Volume over CEC depends on the TV and soundbar.

This is the most universal option and avoids each TV maker's developer-mode hoops.

---

## Things that apply to every TV

- **Find the TV by MAC address,** not IP; or set a DHCP reservation on the router.
- **macOS asks for Local Network permission** the first time Python talks to the TV. If the TV
  is never found, that's the first thing to check.
- **Pairing prompts appear on the TV;** someone has to press Allow with the remote.
- **Key presses can't see the screen.** Menu navigation by keys (like `tools/lounge-launch.json`)
  breaks when the menu layout changes. Prefer real APIs (launch by app id, set input by name)
  wherever the platform has them.
- **Be gentle while people are watching:**
  - Test with volume or info keys first.
  - Never power off without asking.
  - Cap relative volume jumps.
