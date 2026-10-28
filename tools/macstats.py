"""Live Mac stats for the Jarvis theme, using only macOS's built-in commands.

Sampling runs in a background thread, and only while something has asked for
stats in the last 30 seconds, so an idle server costs nothing.
"""
import re
import subprocess
import threading
import time

IDLE_AFTER = 30
INTERVAL = 3


def _run(*cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _cpu_and_top():
    # Two samples one second apart; the second has real percentages.
    out = _run("top", "-l", "2", "-s", "1", "-n", "6", "-o", "cpu", "-stats", "command,cpu", timeout=8)
    sample = out.split("Processes:")[-1]
    cpu = None
    m = re.search(r"CPU usage:\s*([\d.]+)% user,\s*([\d.]+)% sys", sample)
    if m:
        cpu = round(float(m.group(1)) + float(m.group(2)))
    top = []
    lines = sample.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("COMMAND"):
            for row in lines[i + 1:i + 7]:
                parts = row.rsplit(None, 1)
                if len(parts) == 2 and parts[0].strip() != "top" and len(top) < 5:
                    try:
                        top.append({"name": parts[0].strip()[:16], "cpu": round(float(parts[1]), 1)})
                    except ValueError:
                        pass
            break
    return cpu, top


def _memory():
    total = int(_run("sysctl", "-n", "hw.memsize").strip() or 0)
    vm = _run("vm_stat")
    page = int((re.search(r"page size of (\d+)", vm) or [0, 16384])[1])

    def pages(label):
        m = re.search(label + r":\s+(\d+)", vm)
        return int(m.group(1)) if m else 0

    used = (pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")) * page
    gb = 1024 ** 3
    return {"usedGB": round(used / gb, 1), "totalGB": round(total / gb, 1),
            "pct": round(100 * used / total) if total else None}


def _battery():
    out = _run("pmset", "-g", "batt")
    m = re.search(r"(\d+)%;\s*([a-zA-Z ]+);", out)
    if not m:
        return None
    return {"pct": int(m.group(1)), "state": m.group(2).strip(), "ac": "AC Power" in out}


def _uptime():
    m = re.search(r"sec = (\d+)", _run("sysctl", "-n", "kern.boottime"))
    return int(time.time()) - int(m.group(1)) if m else None


def _load():
    nums = re.findall(r"[\d.]+", _run("sysctl", "-n", "vm.loadavg"))
    return [float(n) for n in nums[:3]]


def _net_bytes(iface="en0"):
    for line in _run("netstat", "-ib", "-I", iface).splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 10 and cols[0] == iface and cols[2].startswith("<Link"):
            return int(cols[6]), int(cols[9])
    return None


class MacStats:
    def __init__(self):
        self.host = _run("scutil", "--get", "ComputerName").strip() or "Mac"
        self._latest = {"host": self.host, "ready": False}
        self._asked = 0.0
        self._lock = threading.Lock()
        self._prev_net = None
        threading.Thread(target=self._loop, daemon=True).start()

    def get(self):
        self._asked = time.time()
        with self._lock:
            return dict(self._latest)

    def _loop(self):
        while True:
            if time.time() - self._asked > IDLE_AFTER:
                time.sleep(1)
                continue
            started = time.time()
            cpu, top = _cpu_and_top()
            net = _net_bytes()
            rx = tx = None
            if net and self._prev_net:
                dt = started - self._prev_net[2]
                rx = max(0, (net[0] - self._prev_net[0]) / dt)
                tx = max(0, (net[1] - self._prev_net[1]) / dt)
            if net:
                self._prev_net = (net[0], net[1], started)
            snap = {
                "host": self.host, "ready": True, "cpu": cpu, "top": top,
                "mem": _memory(), "battery": _battery(), "uptimeSec": _uptime(),
                "load": _load(), "rxBps": rx, "txBps": tx, "at": int(time.time() * 1000),
            }
            with self._lock:
                self._latest = snap
            time.sleep(max(0.5, INTERVAL - (time.time() - started)))
