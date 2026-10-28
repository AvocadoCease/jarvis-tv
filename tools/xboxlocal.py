"""Local Xbox control over SmartGlass (UDP 5050), no Microsoft sign-in needed.

- status():   is the console awake? (discovery ping, cached briefly)
- power_on(): wake it; works when the console's power mode is Sleep / Instant-on

Turning it off or pressing buttons needs an authenticated session, which this
module deliberately doesn't do.
"""
import re
import socket
import struct
import subprocess
import time

import settings

XBOX_MAC = settings.XBOX.get("mac", "")
PORT = 5050

# The Live ID (read from the console while it's awake) lets power_on() work while it's asleep.
_state = {"ip": settings.XBOX.get("ip") or None, "live_id": settings.XBOX.get("live_id") or None, "online": None, "checked": 0.0, "name": None}


def _norm_mac(mac):
    return ":".join(p.zfill(2) for p in mac.lower().split(":"))


def _arp_ip():
    out = subprocess.run(["arp", "-an"], capture_output=True, text=True, timeout=5).stdout
    for line in out.splitlines():
        m = re.search(r"\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-f:]+)", line)
        if m and _norm_mac(m.group(2)) == XBOX_MAC and not m.group(1).endswith(".255"):
            return m.group(1)
    return None


def _discovery_packet():
    payload = struct.pack(">IHHH", 0, 3, 0, 2)  # flags, client=Windows, min/max version
    return struct.pack(">HHH", 0xDD00, len(payload), 0) + payload


def _sg_string(buf, off):
    n = struct.unpack(">H", buf[off:off + 2])[0]
    return buf[off + 2:off + 2 + n].decode(errors="ignore"), off + 2 + n + 1


def _common_name(cert):
    """The console's Live ID: the subject CN in its DER certificate.

    The issuer ("XBL Smart Glass Issuing CA") is a CN too, so take the one that
    looks like a Live ID.
    """
    i = cert.find(b"\x55\x04\x03")
    while i >= 0:
        length = cert[i + 4]
        value = cert[i + 5:i + 5 + length].decode(errors="ignore")
        if re.fullmatch(r"[A-Z0-9]{16}", value):
            return value
        i = cert.find(b"\x55\x04\x03", i + 3)
    return None


def _parse(d):
    p = d[6:]
    off = 6
    name, off = _sg_string(p, off)
    _uuid, off = _sg_string(p, off)
    off += 4  # last error
    clen = struct.unpack(">H", p[off:off + 2])[0]
    return name, _common_name(p[off + 2:off + 2 + clen])


def status(max_age=8):
    """True if the Xbox answered discovery (awake), False if not."""
    if time.time() - _state["checked"] < max_age and _state["online"] is not None:
        return _state["online"]
    ip = _arp_ip() or _state["ip"] or "255.255.255.255"   # no address yet: ask the whole network
    online = False
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(1.2)
    try:
        s.sendto(_discovery_packet(), (ip, PORT))
        d, addr = s.recvfrom(4096)
        if struct.unpack(">H", d[:2])[0] == 0xDD01:
            online = True
            _state["ip"] = addr[0]
            _state["name"], live_id = _parse(d)
            if live_id:
                _state["live_id"] = live_id
    except (socket.timeout, OSError, struct.error, IndexError):
        pass
    finally:
        s.close()
    _state.update(online=online, checked=time.time())
    return online


def info():
    return {"online": _state["online"], "ip": _state["ip"], "name": _state["name"], "liveId": _state["live_id"]}


def power_on(tries=5):
    """Send the SmartGlass power-on packet a few times to the console and the broadcast address."""
    if not _state["live_id"]:
        status(max_age=0)
    live_id = _state["live_id"]
    if not live_id:
        raise RuntimeError("I don't know the Xbox's ID yet; it needs to be on once while I'm watching")
    lid = live_id.encode()
    payload = struct.pack(">H", len(lid)) + lid + b"\x00"
    pkt = struct.pack(">HHH", 0xDD02, len(payload), 0) + payload
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        for _ in range(tries):
            for target in filter(None, (_state["ip"], "255.255.255.255")):
                try:
                    s.sendto(pkt, (target, PORT))
                except OSError:
                    pass
            time.sleep(0.15)
    finally:
        s.close()
    _state["checked"] = 0  # re-check on next status()


def wait_online(timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        if status(max_age=0):
            return True
        time.sleep(1.5)
    return False


if __name__ == "__main__":
    print("online:", status(max_age=0), info())
