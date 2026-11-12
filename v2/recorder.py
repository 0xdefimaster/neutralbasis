"""The V2 selective recorder: raw public observations of verified multi-venue markets
only, append-only, compressed, hashed, with gaps marked and a disk guard.

PAPER / research. Never on E: (failing disk). Layout under `root`:
  segments/<YYYYMMDD>/<n>.jsonl.gz      sealed segments (immutable)
  segments/<YYYYMMDD>/<n>.part.jsonl.gz the open segment (sealed on rotation or stop;
                                        on start, a leftover one is sealed as
                                        "recovered", never deleted)
  manifest.jsonl                         one line per sealed segment: sha256, bytes,
                                         lines, first/last received, recovered flag
  gaps.jsonl                             one line per known gap (venue down, stale,
                                         recorder stopped)
Each record: schema, kind, venue, asset, received (UTC epoch s), source_ts, payload.
A record for a (venue, asset) that is not wanted (not a V2 multi-venue row) is
refused. When free disk space falls under `min_free_bytes` the recorder stops writing,
marks a gap and reports it; it never fills the disk.
"""
import datetime
import gzip
import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path

SCHEMA = 1


class Recorder:
    def __init__(self, root, wanted, min_free_bytes=2 * 1024 ** 3,
                 segment_bytes=64 * 1024 ** 2, segment_seconds=3600.0, clock=time.time,
                 disk_usage=shutil.disk_usage):
        root = Path(root).resolve()
        if str(root).lower().startswith("e:"):
            raise ValueError("the recorder must not write on E: (failing disk)")
        self.root, self.wanted = root, wanted
        self.min_free, self.segment_bytes = min_free_bytes, segment_bytes
        self.segment_seconds, self.clock, self.disk_usage = segment_seconds, clock, disk_usage
        self.lock = threading.Lock()
        self.fh = self.part = None
        self.opened = self.first = self.last = None
        self.lines = 0
        self.raw_bytes = 0
        self.stopped_for_disk = False
        self.stats = dict(written=0, refused_unwanted=0, refused_disk=0, sealed=0,
                          recovered=0, gaps=0)
        (self.root / "segments").mkdir(parents=True, exist_ok=True)
        self.recover()

    # -- files ------------------------------------------------------------------------
    def recover(self):
        for p in sorted((self.root / "segments").glob("*/*.part.jsonl.gz")):
            self._seal(p, recovered=True)
            self.stats["recovered"] += 1

    def _open(self):
        day = datetime.datetime.fromtimestamp(self.clock(), datetime.timezone.utc).strftime(
            "%Y%m%d")
        d = self.root / "segments" / day
        d.mkdir(parents=True, exist_ok=True)
        n = len(list(d.glob("*.jsonl.gz")))
        self.part = d / ("%06d.part.jsonl.gz" % n)
        self.fh = gzip.open(self.part, "at", encoding="utf-8")
        self.opened, self.first, self.lines, self.raw_bytes = self.clock(), None, 0, 0

    def _seal(self, part, recovered=False):
        data = part.read_bytes()
        lines = first = last = None
        try:
            text = gzip.decompress(data).decode("utf-8", "replace").splitlines()
            lines = len(text)
            recs = [json.loads(t) for t in text if t.strip()]
            first = recs[0]["received"] if recs else None
            last = recs[-1]["received"] if recs else None
        except Exception:
            pass  # a torn tail from a crash: kept as is, flagged recovered
        final = part.with_name(part.name.replace(".part.jsonl.gz", ".jsonl.gz"))
        os.replace(part, final)
        with open(self.root / "manifest.jsonl", "a", encoding="utf-8") as m:
            m.write(json.dumps(dict(file=str(final.relative_to(self.root)),
                                    sha256=hashlib.sha256(data).hexdigest(),
                                    bytes=len(data), lines=lines, first=first, last=last,
                                    recovered=recovered, sealed=self.clock())) + "\n")
        self.stats["sealed"] += 1

    def rotate(self):
        with self.lock:
            self._rotate_locked()

    def _rotate_locked(self):
        if self.fh is not None:
            self.fh.close()
            self._seal(self.part)
            self.fh = self.part = None

    def close(self):
        self.rotate()

    # -- guards ---------------------------------------------------------------------------
    def disk_ok(self):
        free = self.disk_usage(str(self.root)).free
        return free >= self.min_free, free

    def gap(self, venue, asset, start, end, reason):
        with open(self.root / "gaps.jsonl", "a", encoding="utf-8") as g:
            g.write(json.dumps(dict(venue=venue, asset=asset, start=start, end=end,
                                    reason=reason)) + "\n")
        self.stats["gaps"] += 1

    # -- write ----------------------------------------------------------------------------
    def record(self, kind, venue, asset, payload, source_ts=None):
        """True when written; False when refused (unwanted market or disk guard)."""
        if (venue, asset) not in self.wanted():
            self.stats["refused_unwanted"] += 1
            return False
        ok, free = self.disk_ok()
        if not ok:
            if not self.stopped_for_disk:
                self.stopped_for_disk = True
                self.rotate()
                self.gap(None, None, self.clock(), None,
                         "disk guard: %d bytes free < %d" % (free, self.min_free))
            self.stats["refused_disk"] += 1
            return False
        if self.stopped_for_disk:
            self.stopped_for_disk = False
            self.gap(None, None, None, self.clock(), "disk guard released")
        rec = dict(schema=SCHEMA, kind=kind, venue=venue, asset=asset,
                   received=self.clock(), source_ts=source_ts, payload=payload)
        line = json.dumps(rec, separators=(",", ":"), default=str) + "\n"
        with self.lock:
            if self.fh is None:
                self._open()
            self.fh.write(line)
            self.lines += 1
            self.raw_bytes += len(line)
            self.first = self.first or rec["received"]
            if (self.raw_bytes >= self.segment_bytes
                    or self.clock() - self.opened >= self.segment_seconds):
                self._rotate_locked()
        self.stats["written"] += 1
        return True

    # -- retention / health -------------------------------------------------------------
    def retention(self, max_age_days=None, max_total_bytes=None):
        """Delete the oldest SEALED segments past the limits; returns the files removed.
        Open segments, the manifest and the gap index are never deleted."""
        sealed = sorted(p for p in (self.root / "segments").glob("*/*.jsonl.gz")
                        if ".part." not in p.name)
        removed = []
        now = self.clock()
        if max_age_days is not None:
            for p in sealed:
                if now - p.stat().st_mtime > max_age_days * 86400:
                    removed.append(p)
        if max_total_bytes is not None:
            keep = [p for p in sealed if p not in removed]
            total = sum(p.stat().st_size for p in keep)
            for p in keep:
                if total <= max_total_bytes:
                    break
                total -= p.stat().st_size
                removed.append(p)
        for p in removed:
            p.unlink()
        return removed

    def health(self):
        ok, free = self.disk_ok()
        return dict(root=str(self.root), disk_free=free, disk_ok=ok,
                    writing=not self.stopped_for_disk, open_segment=str(self.part)
                    if self.part else None, **self.stats)
