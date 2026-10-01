"""How much CPU and memory Stowaway saves by keeping apps asleep.

A stopped container uses nothing, so what's saved is what it *would* have used
sitting idle. Stowaway learns that per app while it's awake and not busy (a
running average of its CPU and memory), then counts it as saved for every
second the app is asleep. The figures are estimates and are labelled as such.

Kept in config/savings.json.
"""
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

LEARN_AFTER_START = 60      # ignore the first minute after a start (start-up spikes)
KNOWN_AFTER = 6             # samples before an app's idle usage counts
MAX_GAP = 60                # never credit more than this many seconds per check
KEEP_DAYS = 400


def host_info():
    """CPU count, total memory (MB) and max clock (MHz) of the server."""
    ncpu = os.cpu_count() or 1
    mem_mb = 0
    try:
        m = re.search(r"MemTotal:\s+(\d+) kB", Path("/proc/meminfo").read_text())
        mem_mb = int(m.group(1)) / 1024 if m else 0
    except OSError:
        pass
    mhz = 0.0
    try:
        mhz = int(Path("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq").read_text()) / 1000
    except (OSError, ValueError):
        try:
            found = re.findall(r"cpu MHz\s*:\s*([\d.]+)", Path("/proc/cpuinfo").read_text())
            mhz = max(float(x) for x in found) if found else 0.0
        except (OSError, ValueError):
            pass
    return {"ncpu": ncpu, "mem_mb": round(mem_mb), "mhz": round(mhz)}


class Savings:
    def __init__(self, path: Path, write):
        self.path = path
        self.write = write          # function(path, text) that writes a private file
        self.data = {"since": time.time(), "baselines": {}, "totals": {}, "days": {}}
        self.dirty = False
        self.saved_at = 0.0
        try:
            loaded = json.loads(path.read_text())
            self.data.update({k: loaded[k] for k in self.data if k in loaded})
        except (OSError, ValueError):
            pass

    # ---- learning ----
    def learn(self, name: str, cpu_pct: float, mem_mb: float | None):
        b = self.data["baselines"].setdefault(name, {"cpu": cpu_pct, "mem": mem_mb or 0.0, "n": 0})
        a = 0.25 if b["n"] < 20 else 0.05          # settle quickly, then drift slowly
        b["cpu"] = b["cpu"] + a * (cpu_pct - b["cpu"])
        if mem_mb is not None:
            b["mem"] = b["mem"] + a * (mem_mb - b["mem"]) if b["n"] else mem_mb
        b["n"] += 1
        self.dirty = True

    def baseline(self, name: str):
        b = self.data["baselines"].get(name)
        return b if b and b["n"] >= KNOWN_AFTER else None

    # ---- counting ----
    def credit(self, name: str, seconds: float, now: float):
        b = self.baseline(name)
        if not b or seconds <= 0:
            return
        seconds = min(seconds, MAX_GAP)
        cpu_s = b["cpu"] / 100 * seconds             # core-seconds
        mem_s = b["mem"] * seconds                   # MB-seconds
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        for bucket in (self.data["totals"], self.data["days"].setdefault(day, {})):
            bucket["cpu_s"] = bucket.get("cpu_s", 0.0) + cpu_s
            bucket["mem_mbs"] = bucket.get("mem_mbs", 0.0) + mem_s
            bucket["asleep_s"] = bucket.get("asleep_s", 0.0) + seconds
        self.dirty = True

    def flush(self, force=False):
        if not self.dirty or (not force and time.time() - self.saved_at < 60):
            return
        days = sorted(self.data["days"])
        for d in days[:-KEEP_DAYS]:
            del self.data["days"][d]
        self.write(self.path, json.dumps(self.data))
        self.dirty, self.saved_at = False, time.time()

    # ---- reporting ----
    def period(self, days: int | None):
        if days is None:
            src = [self.data["totals"]]
        else:
            today = datetime.now()
            keys = {datetime.fromtimestamp(today.timestamp() - i * 86400).strftime("%Y-%m-%d") for i in range(days)}
            src = [v for k, v in self.data["days"].items() if k in keys]
        return {k: sum(b.get(k, 0.0) for b in src) for k in ("cpu_s", "mem_mbs", "asleep_s")}
