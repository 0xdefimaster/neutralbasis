"""Out-of-engine recorder for the RFQ allocation trace (PAPER ONLY; read-only).

Polls GET /api/rfq-trace on a loopback address and appends JSONL records:
  {"type": "cycle", ...}  one trace cycle as served
  {"type": "event", ...}  one stream lifecycle event
  {"type": "meta",  ...}  recorder facts: start, engine identity changes, lost or
                          repeated cycle/event ids, truncated pages, errors, disk limits

It never opens the engine database, never writes next to it, and keeps only what
the endpoint serves (already whitelisted) after a second redaction pass.

Disk limits: one file is rotated at --max-file-mb; files beyond --max-total-mb or
older than --keep-days are deleted (oldest first); writing pauses while the target
disk has less than --min-free-gb free (C: is small; the default is conservative).

Usage (PowerShell, from the repository root):
  python tools\\rfq_trace_recorder.py --out C:\\Users\\W10\\nb-trace --interval 60
"""

import argparse, json, os, re, shutil, sys, time, urllib.error, urllib.parse, urllib.request

LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")
MIN_INTERVAL = 30.0
FORBIDDEN_DIRS = ("data-five-rh", "data", "data-railway")
DROP_KEYS = {"raw", "payload", "body", "bids", "asks", "headers", "authorization",
             "api_key", "apikey", "secret", "token", "signature", "password", "key"}
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|signature|password|"
               r"authorization|bearer)\b\s*[:=]?\s*[\"']?[A-Za-z0-9._~+/=-]{6,}"),
    re.compile(r"\b0x[0-9a-fA-F]{40,}\b"),                 # long hex (keys, hashes)
    re.compile(r"\b[A-Za-z0-9+/_-]{32,}={0,2}\b"),          # long base64-like blobs
)


def redact_text(s):
    for p in SECRET_PATTERNS:
        s = p.sub("[REDACTED]", s)
    return s


def redact(obj):
    """Copy of `obj` with sensitive keys dropped and secret-looking strings masked."""
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()
                if str(k).lower() not in DROP_KEYS}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def check_url(url):
    u = urllib.parse.urlsplit(url)
    if u.scheme != "http" or (u.hostname or "") not in LOOPBACK:
        raise SystemExit("Yalnızca loopback http adresi kabul edilir: " + url)
    return url.rstrip("/")


def check_out(path):
    p = os.path.abspath(path)
    parts = [x.lower() for x in os.path.normpath(p).split(os.sep)]
    if any(d in parts for d in FORBIDDEN_DIRS) or p.lower().endswith(".sqlite3"):
        raise SystemExit("Kayıt klasörü motorun veri klasörü olamaz: " + p)
    return p


class Sink:
    """Rotating JSONL files with size, total and age limits and a free-space floor."""

    def __init__(self, folder, max_file_mb, max_total_mb, keep_days, min_free_gb,
                 clock=time.time, disk_usage=shutil.disk_usage):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.max_file = int(max_file_mb * 1e6)
        self.max_total = int(max_total_mb * 1e6)
        self.keep = keep_days * 86400
        self.min_free = int(min_free_gb * 1e9)
        self.clock = clock
        self.disk_usage = disk_usage
        self.path = None
        self.paused = False
        self.dropped = 0

    def files(self):
        return sorted(
            os.path.join(self.folder, n) for n in os.listdir(self.folder)
            if n.startswith("rfq-trace-") and n.endswith(".jsonl")
        )

    def _new_path(self):
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
        n, path = 0, os.path.join(self.folder, "rfq-trace-%s.jsonl" % stamp)
        while os.path.exists(path):
            n += 1
            path = os.path.join(self.folder, "rfq-trace-%s-%d.jsonl" % (stamp, n))
        return path

    def enforce(self):
        """Delete the oldest files beyond the age and total-size limits (never the
        file being written)."""
        now = self.clock()
        files = self.files()
        for f in files:
            if f != self.path and now - os.path.getmtime(f) > self.keep:
                os.remove(f)
        files = self.files()
        total = sum(os.path.getsize(f) for f in files)
        for f in files:
            if total <= self.max_total or f == self.path:
                break
            total -= os.path.getsize(f)
            os.remove(f)

    def write(self, records):
        free = self.disk_usage(self.folder).free
        if free < self.min_free:
            self.paused = True
            self.dropped += len(records)
            return False
        self.paused = False
        if self.path is None or (os.path.exists(self.path)
                                 and os.path.getsize(self.path) >= self.max_file):
            self.path = self._new_path()
            self.enforce()
        with open(self.path, "a", encoding="utf8", newline="\n") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n")
        self.enforce()
        return True


class Recorder:
    def __init__(self, base_url, sink, interval=60.0, fetch=None, clock=time.time,
                 sleep=time.sleep, password=None):
        self.base = check_url(base_url)
        self.sink = sink
        self.interval = max(MIN_INTERVAL, float(interval))
        self.fetch = fetch or self._fetch
        self.clock = clock
        self.sleep = sleep
        self.password = password
        self.last_cycle = 0
        self.last_event = 0
        self.identity = None
        self.backoff = 0.0
        self.stats = dict(polls=0, cycles=0, events=0, lost_cycles=0, lost_events=0,
                          repeated=0, truncated_pages=0, errors=0, restarts=0)

    def _fetch(self, path):
        req = urllib.request.Request(self.base + path)
        if self.password:
            import base64

            raw = base64.b64encode(("paper:" + self.password).encode()).decode()
            req.add_header("Authorization", "Basic " + raw)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)

    def meta(self, kind, **fields):
        return dict(type="meta", kind=kind, at=self.clock(), **redact(fields))

    def poll_once(self):
        """One poll: identity check, then pages until the engine has nothing newer."""
        out = []
        ident = self.fetch("/api/identity")
        if ident.get("mode") != "PAPER":
            raise RuntimeError("Motor PAPER modunda değil; kayıt yapılmadı")
        key = (ident.get("pid"), ident.get("root"))
        if self.identity is not None and key != self.identity:
            # A restarted engine starts its ids again from 1.
            self.stats["restarts"] += 1
            out.append(self.meta("engine_restart", previous_pid=self.identity[0],
                                 pid=key[0]))
            self.last_cycle = self.last_event = 0
        if self.identity is None:
            out.append(self.meta("recorder_start", pid=key[0], interval=self.interval))
        self.identity = key
        for _ in range(50):  # bounded paging per poll
            page = self.fetch("/api/rfq-trace?since_cycle=%d&since_event=%d"
                              % (self.last_cycle, self.last_event))
            if page.get("mode") != "PAPER":
                raise RuntimeError("İz yanıtı PAPER değil")
            if page.get("truncated"):
                self.stats["truncated_pages"] += 1
                out.append(self.meta("truncated_page", since_cycle=self.last_cycle))
            cycles, events = page.get("cycles") or [], page.get("events") or []
            for c in cycles:
                cid = c.get("cycle_id", 0)
                if cid <= self.last_cycle:
                    self.stats["repeated"] += 1
                    continue
                if cid > self.last_cycle + 1:
                    # Cycles that left the engine's ring before this read.
                    lost = cid - self.last_cycle - 1
                    self.stats["lost_cycles"] += lost
                    out.append(self.meta("lost_cycles", after=self.last_cycle,
                                         next=cid, count=lost))
                out.append(dict(redact(c), type="cycle", pid=key[0]))
                self.last_cycle = cid
                self.stats["cycles"] += 1
            for e in events:
                eid = e.get("id", 0)
                if eid <= self.last_event:
                    self.stats["repeated"] += 1
                    continue
                if eid > self.last_event + 1:
                    lost = eid - self.last_event - 1
                    self.stats["lost_events"] += lost
                    out.append(self.meta("lost_events", after=self.last_event,
                                         next=eid, count=lost))
                out.append(dict(redact(e), type="event", pid=key[0]))
                self.last_event = eid
                self.stats["events"] += 1
            if not cycles and not events:
                break
        self.stats["polls"] += 1
        return out

    def run(self, polls=None):
        n = 0
        while polls is None or n < polls:
            n += 1
            try:
                records = self.poll_once()
                self.backoff = 0.0
            except (OSError, ValueError, RuntimeError, urllib.error.URLError) as ex:
                self.stats["errors"] += 1
                records = [self.meta("error", error=redact_text(
                    "%s: %s" % (type(ex).__name__, str(ex)[:200])))]
                # Bounded backoff while the engine is unreachable.
                self.backoff = min(600.0, max(self.interval, self.backoff * 2 or self.interval))
            if records and not self.sink.write(records):
                print("Boş alan sınırın altında; kayıt duraklatıldı", file=sys.stderr)
            self.sleep(self.backoff or self.interval)
        return self.stats


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default="http://127.0.0.1:8765")
    p.add_argument("--out", required=True)
    p.add_argument("--interval", type=float, default=60.0)
    p.add_argument("--max-file-mb", type=float, default=50)
    p.add_argument("--max-total-mb", type=float, default=500)
    p.add_argument("--keep-days", type=float, default=7)
    p.add_argument("--min-free-gb", type=float, default=10)
    p.add_argument("--polls", type=int, default=None)
    a = p.parse_args(argv)
    sink = Sink(check_out(a.out), a.max_file_mb, a.max_total_mb, a.keep_days, a.min_free_gb)
    rec = Recorder(a.url, sink, a.interval,
                   password=os.environ.get("NB_DASHBOARD_PASSWORD") or None)
    stats = rec.run(a.polls)
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
