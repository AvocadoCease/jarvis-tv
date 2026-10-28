"""Control a 2012-2015 Samsung Smart TV (written for a UA46ES7500) from the Mac.

- find():        locate the TV by its MAC address (its DHCP address changes)
- keys([...]):   press remote buttons (legacy protocol, port 55000)
- play_url():    show/play media via DLNA (port 7676)
- cast_start():  stream this Mac's screen to the TV; cast_stop() ends it
"""
import base64
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from xml.sax.saxutils import escape

import settings

TV_MAC = settings.TV.get("mac", "")
MY_ID = "lounge-mac"
NAME = "Lounge (Mac)"
APP = b"iphone..iapp.samsung"
TV_APP = f"iphone.{settings.TV.get('model') or 'TV'}.iapp.samsung".encode()
AVTRANSPORT = "/smp_24_"
RENDERING = "/smp_18_"
CAST_PORT = 8766
TOOLS = os.path.dirname(os.path.abspath(__file__))

_cache = {"ip": None}
_lock = threading.Lock()


# ------------------------------------------------------------------ discovery

def _norm_mac(mac):
    return ":".join(p.zfill(2) for p in mac.lower().split(":"))


def my_ip():
    try:
        return subprocess.run(["ipconfig", "getifaddr", "en0"], capture_output=True, text=True,
                              timeout=3).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _arp_candidates():
    """Every IP the ARP table maps to the TV's MAC (stale entries linger after DHCP changes)."""
    out = subprocess.run(["arp", "-an"], capture_output=True, text=True, timeout=5).stdout
    ips = []
    for line in out.splitlines():
        m = re.search(r"\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-f:]+)", line)
        if m and _norm_mac(m.group(2)) == TV_MAC and not m.group(1).endswith(".255"):
            ips.append(m.group(1))
    return ips


def _first_alive(ips):
    for ip in ips:
        if _alive(ip):
            return ip
    return None


def _sweep(prefix):
    procs = [subprocess.Popen(["ping", "-c1", "-W", "300", f"{prefix}.{i}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for i in range(1, 255)]
    for p in procs:
        p.wait()


def _alive(ip):
    try:
        socket.create_connection((ip, 55000), timeout=1).close()
        return True
    except OSError:
        return False


def find(refresh=False):
    """The TV's current IP, or None if it is off / unreachable."""
    with _lock:
        ip = _cache["ip"]
        if ip and not refresh and _alive(ip):
            return ip
        ip = _first_alive(_arp_candidates())
        if not ip:
            # The TV's DHCP address keeps changing: sweep the subnet so its new
            # address lands in the ARP table, then give the table a moment.
            mine = my_ip()
            if mine:
                _sweep(mine.rsplit(".", 1)[0])
                for _ in range(3):
                    ip = _first_alive(_arp_candidates())
                    if ip:
                        break
                    time.sleep(1)
        _cache["ip"] = ip
        return ip


# ------------------------------------------------------------------ remote keys

def _field(data):
    return struct.pack("<H", len(data)) + data


def _b64(text):
    return base64.b64encode(text.encode())


def _connect(ip):
    s = socket.create_connection((ip, 55000), timeout=5)
    payload = b"\x64\x00" + _field(_b64(my_ip() or "0.0.0.0")) + _field(_b64(MY_ID)) + _field(_b64(NAME))
    s.sendall(b"\x00" + _field(APP) + _field(payload))
    deadline = time.time() + 30
    while time.time() < deadline:
        s.settimeout(max(1, deadline - time.time()))
        head = s.recv(3)
        if len(head) < 3:
            break
        s.recv(struct.unpack("<H", head[1:3])[0])
        body = s.recv(struct.unpack("<H", s.recv(2))[0])
        if body.startswith(b"\x64\x00\x01\x00"):
            return s
        if body.startswith(b"\x64\x00\x00\x00") or body.startswith(b"\x65\x00"):
            break
        # b"\x0a\x00\x02..." means the TV is showing Allow/Deny: keep waiting
    s.close()
    raise RuntimeError("TV did not allow remote control")


def keys(names, gap=0.35):
    """Press buttons. A "WAIT:<seconds>" entry pauses mid-sequence (e.g. while Smart Hub loads)."""
    ip = find()
    if not ip:
        raise RuntimeError("TV is off or not on the network")
    s = _connect(ip)
    try:
        for k in names:
            if k.startswith("WAIT:"):
                time.sleep(float(k[5:]))
                continue
            s.sendall(b"\x00" + _field(TV_APP) + _field(b"\x00\x00\x00" + _field(_b64(k))))
            time.sleep(gap)
    finally:
        s.close()


# ------------------------------------------------------------------ DLNA media

def _soap(action, body, path=AVTRANSPORT, service="AVTransport"):
    ip = find()
    if not ip:
        raise RuntimeError("TV is off or not on the network")
    # Standard UPnP media services vs Samsung's own (MainTVAgent2): different
    # namespace, and only the media services take an InstanceID.
    upnp = service in ("AVTransport", "RenderingControl")
    urn = f"urn:schemas-upnp-org:service:{service}:1" if upnp else f"urn:samsung.com:service:{service}:1"
    envelope = ('<?xml version="1.0" encoding="utf-8"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{urn}">{"<InstanceID>0</InstanceID>" if upnp else ""}'
                f'{body}</u:{action}></s:Body></s:Envelope>').encode()
    # http.client rather than urllib, which rewrites "SOAPACTION" as "Soapaction".
    import http.client
    conn = http.client.HTTPConnection(ip, 7676, timeout=8)
    try:
        conn.putrequest("POST", path, skip_accept_encoding=True)
        conn.putheader("Content-Type", 'text/xml; charset="utf-8"')
        conn.putheader("SOAPACTION", f'"{urn}#{action}"')
        conn.putheader("Content-Length", str(len(envelope)))
        conn.endheaders(envelope)
        return conn.getresponse().read().decode(errors="ignore")
    finally:
        conn.close()


def play_url(url, title="Lounge", kind="video"):
    """kind: video | image | audio"""
    upnp = {"video": "object.item.videoItem", "image": "object.item.imageItem.photo",
            "audio": "object.item.audioItem.musicTrack"}[kind]
    mime = {"video": "video/mpeg", "image": "image/jpeg", "audio": "audio/mpeg"}[kind]
    flags = "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000" if kind == "video" else "*"
    didl = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            f'<item id="1" parentID="0" restricted="1"><dc:title>{escape(title)}</dc:title>'
            f'<upnp:class>{upnp}</upnp:class><res protocolInfo="http-get:*:{mime}:{flags}">{escape(url)}</res>'
            '</item></DIDL-Lite>')
    try:
        _soap("Stop", "")
    except Exception:
        pass
    _soap("SetAVTransportURI", f"<CurrentURI>{escape(url)}</CurrentURI><CurrentURIMetaData>{escape(didl)}</CurrentURIMetaData>")
    _soap("Play", "<Speed>1</Speed>")


def stop_media():
    _soap("Stop", "")


# ------------------------------------------------------------------ inputs (MainTVAgent2)

MAINTV = "/smp_8_"


def app_in_front():
    """True when a Smart Hub app is on screen: the TV then reports no current input."""
    try:
        out = _soap("GetCurrentExternalSource", "", MAINTV, "MainTVAgent2")
    except Exception:
        return True  # the source service goes quiet while an app runs
    m = re.search(r"<CurrentExternalSource>([^<]*)</CurrentExternalSource>", out)
    return not (m and m.group(1).strip())


def sources():
    """[{"type": "HDMI3", "id": "69", "name": "", "connected": True}, ...] from the TV itself."""
    out = _soap("GetSourceList", "", MAINTV, "MainTVAgent2").replace("&lt;", "<").replace("&gt;", ">")
    found = []
    for block in re.findall(r"<Source>(.*?)</Source>", out, re.S):
        def get(tag):
            m = re.search(rf"<{tag}>([^<]*)</{tag}>", block)
            return m.group(1) if m else ""
        found.append({"type": get("SourceType"), "id": get("ID"), "name": get("DeviceName"),
                      "connected": get("Connected") == "Yes"})
    return found


def set_source(want):
    """Switch input by type ("HDMI3", "TV") or device name ("XBOX"). Returns the type used."""
    w = want.upper().replace(" ", "")
    for src in sources():
        if w in (src["type"].upper().replace("/DVI", ""), src["name"].upper()):
            out = _soap("SetMainTVSource", f"<Source>{src['type']}</Source><ID>{src['id']}</ID><UiID>{src['id']}</UiID>",
                        MAINTV, "MainTVAgent2")
            if "<Result>OK</Result>" not in out:
                raise RuntimeError(f"TV refused to switch to {src['type']}")
            return src["type"]
    raise RuntimeError(f"no input called {want}")


# ------------------------------------------------------------------ volume (0-100, exact)
# The TV applies SetVolume immediately but GetVolume can lag a second behind.

_volume = {"value": None, "at": 0.0}


def get_volume():
    if time.time() - _volume["at"] < 3 and _volume["value"] is not None:
        return _volume["value"]
    out = _soap("GetVolume", "<Channel>Master</Channel>", RENDERING, "RenderingControl")
    m = re.search(r"<CurrentVolume>(\d+)</CurrentVolume>", out)
    if not m:
        raise RuntimeError("TV did not report its volume")
    return int(m.group(1))


def set_volume(level):
    level = max(0, min(100, int(level)))
    _soap("SetVolume", f"<Channel>Master</Channel><DesiredVolume>{level}</DesiredVolume>", RENDERING, "RenderingControl")
    _volume.update(value=level, at=time.time())
    return level


# ------------------------------------------------------------------ screen cast

_cast = {"server": None, "awake": None}


def casting():
    return _cast["server"] is not None and _cast["server"].poll() is None


def cast_start(fps=50, rate="15M"):
    if not casting():
        # Own process group, so stopping it also stops the ffmpeg it starts per viewer
        _cast["server"] = subprocess.Popen([sys.executable, "-I", os.path.join(TOOLS, "screencast.py"), str(fps), rate],
                                           start_new_session=True)
        # keep the display awake, or the screensaver is what gets streamed
        _cast["awake"] = subprocess.Popen(["caffeinate", "-d"])
        time.sleep(1.5)
    play_url(f"http://{my_ip()}:{CAST_PORT}/screen.ts", title="Mac Screen", kind="video")


def cast_stop():
    try:
        stop_media()
    except Exception:
        pass
    import signal
    for k in ("server", "awake"):
        p = _cast[k]
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL) if k == "server" else p.terminate()
            except ProcessLookupError:
                pass
        _cast[k] = None
    # Anything left from an earlier JARVIS run (e.g. killed mid-cast)
    subprocess.run(["pkill", "-9", "-f", "screencast.py|avfoundation.*4:none"], capture_output=True)


if __name__ == "__main__":
    print("TV at", find())
