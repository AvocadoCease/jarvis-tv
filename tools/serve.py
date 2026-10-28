"""Lounge dev and install server.

  python3 tools/serve.py            # preview at http://<mac>:8780/preview/
  python3 tools/serve.py --port 80  # also what the TV's "User App Sync" reads

Routes:
  /preview/...       the widget, for a desktop browser
  /widgetlist.xml    app list the TV downloads during User App Sync
  /Widget/<zip>      packaged app (built by tools/build.py)
  /log?...           debug beacon from the app on the TV (printed as TVLOG)
  /time              clock fallback for the TV: {"epochMs", "tzOffsetSec"}
  /stats             live Mac stats for the Jarvis theme (see macstats.py)
  /remote            JARVIS remote: buttons + voice (voice needs http://localhost/remote)
  /api/status        {"tv": ip|null, "casting": bool}
  /api/key?k=KEY_X   press remote buttons (comma-separated)
  /api/command?text=&from=  run a JARVIS command or ask a question (see jarvis.py, brain.py);
                     from = mac | remote | tv decides where the answer is shown
  /api/privacy?on=1|0  privacy mode: no email/message contents on the TV
  /api/announce?after=N  new announcements for the Mac to say (see announce.py); ?on=1|0 turns them on/off; ?test=text (Mac only) queues one
  /cast/photo.jpg    the last photo JARVIS put on the TV
  /api/tts?text=     JARVIS's voice as MP3 (see voice.py)
  /api/events        recent JARVIS events (the remote speaks new "notice" ones)
  /setup/xbox        (Mac only) link the Xbox via Microsoft sign-in (see xboxcloud.py)
  /auth/callback     (Mac only) Microsoft sends the sign-in back here
"""
import argparse
import json
import os
import re
import sys
import time
from urllib.parse import parse_qs, urlparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # works under python -I
from macstats import MacStats  # noqa: E402
import announce  # noqa: E402
import brain  # noqa: E402
import jarvis  # noqa: E402
import voice  # noqa: E402
import xboxlocal as xbox  # noqa: E402
import tvcontrol  # noqa: E402
try:
    import xboxcloud  # noqa: E402  (needs the project's .venv)
except ImportError:
    xboxcloud = None
from html import escape  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WIDGET = os.path.join(ROOT, "widget")
DIST = os.path.join(ROOT, "dist")
REMOTE = os.path.join(ROOT, "remote")
STATS = MacStats()
jarvis.get_stats = STATS.get


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path.startswith("/preview/"):
            return os.path.join(WIDGET, path[len("/preview/"):])
        if path == "/widgetlist.xml":
            return os.path.join(DIST, "widgetlist.xml")
        if path.startswith("/Widget/"):
            return os.path.join(DIST, os.path.basename(path))
        if path in ("/remote", "/remote/"):
            return os.path.join(REMOTE, "index.html")
        if path == "/cast/photo.jpg":
            return os.path.join(jarvis.PHOTO_DIR, "photo.jpg")
        return os.path.join(WIDGET, "__missing__")

    def send_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------ Xbox sign-in (Mac only)

    def from_this_mac(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def send_page(self, title, body, status=200):
        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{escape(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{margin:0;min-height:100vh;background:#000;color:#18e0ff;font:15px ui-monospace,monospace;display:flex;justify-content:center;padding:40px 16px}}
main{{max-width:520px;width:100%}} h1{{font:300 32px sans-serif;letter-spacing:3px;margin:0 0 6px}} h1 .r{{color:#dc143c}}
p{{color:#cfefff;line-height:1.6}} label{{display:block;margin:18px 0 6px;font-size:12px;letter-spacing:2px}}
input{{width:100%;box-sizing:border-box;background:#00181f;border:1px solid #18e0ff80;border-radius:4px;color:#e8fdff;font:15px ui-monospace,monospace;padding:11px}}
button{{margin-top:22px;width:100%;height:46px;background:#18e0ff;color:#00141a;border:0;border-radius:4px;font:15px ui-monospace,monospace;letter-spacing:2px;cursor:pointer}}
.ok{{color:#ffa21a}} .err{{color:#dc143c}} code{{color:#e8fdff}}
</style></head><body><main><h1><span class="r">JARV</span>IS</h1>{body}</main></body></html>""".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def setup_xbox_page(self, note=""):
        if xboxcloud is None:
            self.send_page("Xbox setup", "<p class='err'>The server isn't running in the project's .venv, so the Xbox library isn't available.</p>", 500)
            return
        state = ("<p class='ok'>Linked. Sign in again below only if you need to.</p>" if xboxcloud.signed_in() else
                 "<p>Paste the two values from your Azure app registration. They're saved only on this Mac "
                 "(<code>~/.config/lounge</code>, readable by you alone), then Microsoft asks you to sign in.</p>")
        self.send_page("Xbox setup", f"""<p>Link your Xbox</p>{note}{state}
<form method="post" action="/setup/xbox" autocomplete="off">
<label>APPLICATION (CLIENT) ID</label><input name="client_id" required spellcheck="false">
<label>CLIENT SECRET (THE "VALUE")</label><input name="client_secret" type="password" required>
<button>SIGN IN WITH MICROSOFT</button></form>""")

    def do_POST(self):
        route = self.path.split("?", 1)[0]
        if route != "/setup/xbox" or not self.from_this_mac():
            self.send_error(404)
            return
        length = min(int(self.headers.get("Content-Length") or 0), 10000)
        form = parse_qs(self.rfile.read(length).decode(errors="ignore"))
        try:
            xboxcloud.save_app(form.get("client_id", [""])[0], form.get("client_secret", [""])[0])
            url = xboxcloud.auth_url()
        except Exception as e:
            self.setup_xbox_page(f"<p class='err'>{escape(str(e))}</p>")
            return
        self.send_response(303)
        self.send_header("Location", url)
        self.end_headers()

    def auth_callback(self):
        q = parse_qs(urlparse(self.path).query)
        if "error" in q:
            msg = q.get("error_description", q["error"])[0]
            self.setup_xbox_page(f"<p class='err'>Microsoft said: {escape(msg)}</p>")
            return
        try:
            consoles = xboxcloud.finish_sign_in(q.get("code", [""])[0], q.get("state", [""])[0])
        except Exception as e:
            self.setup_xbox_page(f"<p class='err'>{escape(str(e))}</p>")
            return
        rows = "".join(f"<p>&#8250; <b>{escape(c['name'])}</b> ({escape(c['type'])}): {escape(c['power'])}, "
                       f"remote features {'on' if c['remote'] else '<span class=err>OFF</span>'}</p>" for c in consoles)
        jarvis._event("\u203a Xbox linked to Microsoft account", "notice")
        self.send_page("Xbox linked", f"<p class='ok'>Signed in. JARVIS can now control your Xbox.</p>{rows or '<p class=err>No consoles found on this account.</p>'}"
                                      "<p>You can close this tab.</p>")

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route in ("/setup/xbox", "/auth/callback"):
            if not self.from_this_mac():
                self.send_error(404)
            elif route == "/setup/xbox":
                self.setup_xbox_page()
            else:
                self.auth_callback()
            return
        if route == "/time":
            self.send_json({"epochMs": int(time.time() * 1000), "tzOffsetSec": time.localtime().tm_gmtoff})
            return
        if route == "/stats":
            self.send_json(dict(STATS.get(), client=self.client_address[0], events=jarvis.events(),
                                xbox={"online": xbox.status(max_age=10)}, briefing=jarvis.current_briefing(),
                                speakingMs=jarvis.speaking_ms()))
            return
        if route == "/api/tts":
            q = parse_qs(urlparse(self.path).query)
            text = q.get("text", [""])[0]
            try:
                with open(voice.synth(text), "rb") as f:
                    data = f.read()
                if q.get("from", [""])[0] != "mac":  # the Mac speaks for itself; don't animate the TV
                    jarvis.mark_speaking(len(data) * 8 / 128000 + 0.4)  # 128 kbps MP3 -> seconds
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if route.startswith("/api/"):
            self.api(route, parse_qs(urlparse(self.path).query))
            return
        if self.path.startswith("/log?"):
            print("TVLOG", self.path[5:], flush=True)
            self.send_response(204)
            self.end_headers()
            return
        if "$MANAGER_WIDGET" in self.path or "%24MANAGER_WIDGET" in self.path:
            # Samsung API scripts only exist on the TV; keep the preview console quiet.
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path in ("/", "/preview"):
            self.send_response(302)
            self.send_header("Location", "/preview/")
            self.end_headers()
            return
        super().do_GET()

    def api(self, route, q):
        try:
            if route == "/api/status":
                self.send_json({"tv": tvcontrol.find(), "casting": tvcontrol.casting(),
                                "xbox": xbox.status(max_age=10)})
            elif route == "/api/key":
                names = [k for k in ",".join(q.get("k", [])).split(",") if k]
                if not names or not all(re.fullmatch(r"KEY_[A-Z0-9_]{1,24}", k) for k in names):
                    self.send_json({"ok": False, "error": "bad key name"})
                    return
                tvcontrol.keys(names[:30])
                self.send_json({"ok": True})
            elif route == "/api/events":
                self.send_json({"events": jarvis.events()})
            elif route == "/api/command":
                server = "http://" + (tvcontrol.my_ip() or "127.0.0.1")
                source = q.get("from", ["remote"])[0]
                source = source if source in ("mac", "remote", "tv") else "remote"
                self.send_json(jarvis.handle(q.get("text", [""])[0][:500], server, source=source))
            elif route == "/api/announce":
                if "on" in q:
                    brain.set_setting("announce", q["on"][0] in ("1", "true", "on"))
                if "test" in q and self.from_this_mac():   # try it: /api/announce?test=Hello
                    announce._push(q["test"][0][:300])
                after = q.get("after", [""])[0]
                self.send_json(dict(announce.since(int(after) if after.lstrip("-").isdigit() else None),
                                    **({"status": announce.status()} if "status" in q else {})))
            elif route == "/api/privacy":
                if "on" in q:
                    brain.set_privacy(q["on"][0] in ("1", "true", "on"))
                self.send_json({"ok": True, "privacy": brain.privacy()})
            else:
                self.send_json({"ok": False, "error": "unknown route"})
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)})

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        # /stats is polled every few seconds; keep it out of the log.
        if not self.path.startswith(("/stats", "/api/status", "/api/events", "/api/announce", "/auth/callback")):
            super().log_message(fmt, *args)


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # Browsers (Safari especially) hang up mid-response all the time; that's not an error.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8780)
    args = ap.parse_args()
    print(f"Lounge server on http://{args.host}:{args.port}/preview/", flush=True)
    brain.warm_up()  # start Claude now so the first question is quick
    announce.start()
    Server((args.host, args.port), Handler).serve_forever()
