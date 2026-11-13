"""Lightweight health telemetry (Phase 14 §5 / Slice C). PAPER ONLY.

No raw data is stored again: producers (v2.ws_readonly.Books, REST readers) report
EVENTS; Telemetry keeps per-(venue, market, channel) minute buckets (bounded to 24 h)
plus the last-message / last-data-change times, and writes one small heartbeat file
(atomic) plus a bounded history file. The read model and the Doctor read those files;
a panel refresh never reaches a venue.

Times: wall clock (epoch s, UTC) for "when"; time.monotonic() for intervals/cadence, so
a clock step cannot make a cadence negative. A data age (wall now - venue source time)
carries the clock confidence the caller supplies (UNKNOWN by default).

Event kinds: message, data_change, rest_request, refresh_ok, error, http_429,
reconnect, sequence_gap.
"""
import collections
import json
import os
import shutil
import time
from pathlib import Path

from v2 import storage

KINDS = ("message", "data_change", "rest_request", "refresh_ok", "error", "http_429",
         "reconnect", "sequence_gap")
WINDOWS_MIN = (1, 5, 60, 1440)
MAX_BUCKETS = 1440
MIN_FREE_GB = 3.0


class Telemetry:
    def __init__(self, clock=time.time, mono=time.monotonic, clock_confidence="UNKNOWN"):
        self.clock, self.mono = clock, mono
        self.clock_confidence = clock_confidence
        self.keys = {}

    def _k(self, venue, market, channel):
        k = (venue, str(market) if market is not None else None, channel)
        if k not in self.keys:
            self.keys[k] = dict(buckets=collections.deque(maxlen=MAX_BUCKETS),
                                last_message_at=None, last_data_change_at=None,
                                last_source_ts=None, last_mono=None, cadence=None,
                                intervals=collections.deque(maxlen=200))
        return self.keys[k]

    def event(self, venue, market, channel, kind, source_ts=None, n=1):
        if kind not in KINDS:
            raise ValueError("unknown telemetry kind %r" % kind)
        s = self._k(venue, market, channel)
        now, mono = self.clock(), self.mono()
        minute = int(now // 60)
        if not s["buckets"] or s["buckets"][-1][0] != minute:
            s["buckets"].append([minute, collections.Counter()])
        s["buckets"][-1][1][kind] += n
        if kind == "message":
            if s["last_mono"] is not None:
                s["intervals"].append(mono - s["last_mono"])
            s["last_mono"], s["last_message_at"] = mono, now
            if source_ts is not None:
                s["last_source_ts"] = source_ts
        elif kind == "data_change":
            s["last_data_change_at"] = now
            if source_ts is not None:
                s["last_source_ts"] = source_ts

    def snapshot(self):
        now = self.clock()
        minute = int(now // 60)
        out = []
        for (venue, market, channel), s in self.keys.items():
            win = {}
            for w in WINDOWS_MIN:
                c = collections.Counter()
                for m, cnt in s["buckets"]:
                    if minute - m < w:
                        c.update(cnt)
                msgs = c["message"]
                win["%dm" % w] = dict(
                    counts=dict(c), messages_per_s=msgs / (w * 60.0),
                    error_rate=None if msgs + c["error"] == 0 else
                    c["error"] / float(msgs + c["error"]))
            iv = sorted(s["intervals"])
            out.append(dict(
                exchange_id=venue, market_id=market, channel=channel, observed_at=now,
                last_message_at=s["last_message_at"],
                last_data_change_at=s["last_data_change_at"],
                message_age_s=None if s["last_message_at"] is None else
                now - s["last_message_at"],
                data_change_age_s=None if s["last_data_change_at"] is None else
                now - s["last_data_change_at"],
                source_data_age_s=None if s["last_source_ts"] is None else
                now - s["last_source_ts"],
                clock_confidence=self.clock_confidence,
                observed_cadence_s=iv[len(iv) // 2] if iv else None,
                windows=win, time_unit="s"))
        return out


def books_hook(telemetry):
    """A callable for v2.ws_readonly.Books(on_record=...): every WS record becomes
    events (message / data_change / sequence_gap); the record itself is unchanged."""
    def hook(venue, r):
        st = r.get("status")
        if st in ("ok", "duplicate", "stale", "resync", "ignored"):
            telemetry.event(venue, r.get("market"), r.get("channel"), "message",
                            source_ts=r.get("source_ts"))
        if st == "ok" and r.get("market") is not None:
            telemetry.event(venue, r.get("market"), r.get("channel"), "data_change",
                            source_ts=r.get("source_ts"))
        if st == "resync":
            telemetry.event(venue, r.get("market"), r.get("channel"), "sequence_gap")
    return hook


def process_memory_mb():
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]
            c = PMC()
            c.cb = ctypes.sizeof(PMC)
            k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC),
                                                   wintypes.DWORD]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            h = k32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
                return c.WorkingSetSize / 2 ** 20
            return None
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2 ** 20
    except Exception:
        return None


class HeartbeatWriter:
    """heartbeat-<name>.json (atomic, small) + health-history-<name>.jsonl (bounded:
    rotated to .1 at max_bytes; only one rotated file is kept)."""

    def __init__(self, folder, name, every_s=15.0, max_bytes=256 * 1024, clock=time.time):
        self.folder = storage.check_dir(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.name, self.every, self.max_bytes, self.clock = name, every_s, max_bytes, clock
        self.last = None
        self.skipped_low_disk = 0

    @property
    def path(self):
        return self.folder / ("heartbeat-%s.json" % self.name)

    @property
    def history(self):
        return self.folder / ("health-history-%s.jsonl" % self.name)

    def maybe_write(self, telemetry, extra=None, force=False):
        now = self.clock()
        if not force and self.last is not None and now - self.last < self.every:
            return False
        if shutil.disk_usage(self.folder).free / 2 ** 30 < MIN_FREE_GB:
            self.skipped_low_disk += 1
            return False
        snap = telemetry.snapshot()
        hb = dict(at=now, name=self.name, pid=os.getpid(), every_s=self.every,
                  memory_mb=process_memory_mb(), series=snap, **(extra or {}))
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(hb, default=str), encoding="utf-8")
        os.replace(tmp, self.path)
        line = json.dumps(dict(at=now, series=[dict(
            exchange_id=s["exchange_id"], market_id=s["market_id"], channel=s["channel"],
            m1=s["windows"]["1m"]["counts"], message_age_s=s["message_age_s"],
            source_data_age_s=s["source_data_age_s"]) for s in snap]), default=str)
        if self.history.exists() and self.history.stat().st_size + len(line) > self.max_bytes:
            os.replace(self.history, self.history.with_suffix(".jsonl.1"))
        with open(self.history, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        self.last = now
        return True


def read_heartbeat(folder, name, now=None, stale_factor=2.0):
    """dict(status RUNNING / STALE / NOT_RUNNING, age_s, heartbeat) for the read model."""
    now = now or time.time()
    p = Path(folder) / ("heartbeat-%s.json" % name)
    if not p.exists():
        return dict(status="NOT_RUNNING", age_s=None, heartbeat=None)
    try:
        hb = json.loads(p.read_text(encoding="utf-8"))
    except Exception as ex:
        return dict(status="UNREADABLE", age_s=None, heartbeat=None, error=str(ex)[:120])
    age = now - float(hb.get("at", 0))
    return dict(status="RUNNING" if age <= stale_factor * float(hb.get("every_s", 15))
                else "STALE", age_s=age, heartbeat=hb)


def read_history(folder, name, since_s=3600, now=None, max_points=720):
    now = now or time.time()
    p = Path(folder) / ("health-history-%s.jsonl" % name)
    pts = []
    for f in (p.with_suffix(".jsonl.1"), p):
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    x = json.loads(line)
                except Exception:
                    continue
                if now - x.get("at", 0) <= since_s:
                    pts.append(x)
    return pts[-max_points:]
