"""The recording loop: one thread per venue, each with its own task schedule, so a venue
whose API fails never stops the others, and a market that fails never stops the rest
of its venue. Every task writes its progress as a checkpoint, so a restart continues
from the last successful point instead of downloading everything again.

Stopping: Ctrl+C, the duration running out, or a STOP file in the recorder folder
(Phase1-Durdur.cmd creates it). The current request finishes, checkpoints are already
written after each task, and the session is closed.
"""

import json
import logging
import os
import threading
import time

from . import matching
from .clients import LABELS, make_client
from .clients.records import record_key
from .storage import Store, iso

TASKS = ("metadata", "funding", "settlements", "candles", "orderbook")
LIVE = "LIVE_PUBLIC_API"
MOCK_PREFIX = "MOCK:"
LOCK_STALE = 180


class VenueRecorder:
    def __init__(self, client, store, cfg, stop, clock=time.time, log=None):
        self.client = client
        self.venue = client.venue
        self.store = store
        self.cfg = cfg
        self.stop = stop
        self.clock = clock
        self.log = log or logging.getLogger("phase1")
        self.all_markets = []
        self.selected = []
        self.status = dict(
            venue=self.venue,
            label=LABELS.get(self.venue, self.venue),
            tasks={},
            connected=None,
        )
        client.http.stop = stop
        self.next_due = {t: 0.0 for t in TASKS}
        self.period = dict(
            metadata=cfg["metadata_seconds"],
            funding=cfg["funding_seconds"],
            settlements=cfg["settlements_seconds"],
            candles=cfg["candles_seconds"],
            orderbook=cfg["orderbook_seconds"],
        )

    # --- bookkeeping ---------------------------------------------------------------
    def put(self, kind, rows, at):
        new = 0
        for r in rows:
            new += self.store.put_record(kind, self.venue, record_key(kind, r), r, at)
        return new

    def error(self, task, market, ex, at):
        rec = dict(
            venue=self.venue,
            task=task,
            market_id=market,
            error="%s: %s" % (type(ex).__name__, str(ex)[:300]),
            at=at,
        )
        self.store.put_record(
            "error", self.venue, "%s|%s|%d" % (task, market, int(at * 1000)), rec, at
        )
        self.log.warning("%s %s %s: %s", self.venue, task, market or "", rec["error"])

    def mark(self, task, ok, at, note=None):
        x = self.status["tasks"].setdefault(task, dict(ok=0, failed=0))
        x["ok" if ok else "failed"] += 1
        x["last_ok" if ok else "last_error_at"] = at
        if note:
            x["note"] = note
        if ok:
            self.status["connected"] = True
        elif self.status["connected"] is None:
            self.status["connected"] = False
        self.store.set_checkpoint("venue_status:" + self.venue, self.status, at)

    def bump(self, market, stats):
        name = "stats:%s:%s" % (self.venue, market)
        total = self.store.checkpoint(name, {})
        for k, v in (stats or {}).items():
            total[k] = total.get(k, 0) + v
        self.store.set_checkpoint(name, total)

    # --- tasks ---------------------------------------------------------------------
    def task_metadata(self, at):
        markets = self.client.markets(at)
        self.put("market", markets, at)
        self.all_markets = markets
        self.selected = self.client.select(markets)
        self.store.set_checkpoint(
            "markets:" + self.venue,
            [
                dict(asset=m["asset"], market_id=m["market_id"], extra=m["extra"])
                for m in self.selected
            ],
            at,
        )
        return "%d piyasa, %d seçili" % (len(markets), len(self.selected))

    def ensure_markets(self, at):
        if self.selected:
            return
        kept = self.store.checkpoint("markets:" + self.venue)
        if kept:
            self.selected = [
                dict(
                    asset=m["asset"],
                    market_id=m["market_id"],
                    extra=m.get("extra") or {},
                    active=True,
                )
                for m in kept
            ]
            return
        self.task_metadata(at)

    def task_funding(self, at):
        rows = self.client.funding_snapshots(at)
        if not self.cfg.get("all_markets"):
            want = set(self.cfg["assets"])
            rows = [r for r in rows if r["asset"] in want]
        return "%d anlık oran" % self.put("funding_snapshot", rows, at)

    def task_settlements(self, at):
        self.ensure_markets(at)
        new = failed = 0
        for m in self.selected:
            if self.stop.is_set():
                break
            mid = m["market_id"]
            name = "settlements:%s:%s" % (self.venue, mid)
            last = self.store.checkpoint(name)
            # The last two hours are read again: a late-published payment is caught,
            # and the records are idempotent.
            start = last - 2 * 3600 if last else at - self.cfg["backfill_days"] * 86400
            try:
                rows, stats = self.client.settlements(m, start, at, at)
            except Exception as ex:
                failed += 1
                self.error("settlements", mid, ex, at)
                continue
            outcome = {}
            for r in rows:
                o = self.store.put_settlement(r, at)
                outcome[o] = outcome.get(o, 0) + 1
            if outcome.get("correction"):
                self.log.warning(
                    "%s %s: %d ödeme borsa tarafından düzeltildi",
                    self.venue,
                    mid,
                    outcome["correction"],
                )
            self.bump(mid, dict(stats, corrections=outcome.get("correction", 0)))
            new += outcome.get("new", 0)
            if rows:
                self.store.set_checkpoint(name, max(r["settled_at"] for r in rows), at)
            self.match(m, at)
        if failed and failed == len(self.selected):
            raise RuntimeError("Hiçbir piyasanın ödeme geçmişi okunamadı")
        return "%d yeni ödeme, %d piyasada hata" % (new, failed)

    def match(self, m, at):
        mid = m["market_id"]
        since = at - 3 * 86400
        snaps = [
            s
            for s in self.store.records("funding_snapshot", self.venue, since)
            if s["market_id"] == mid
        ]
        paid = [
            s
            for s in self.store.latest("settlement", self.venue, since)
            if s["market_id"] == mid
        ]
        final, pending = matching.compare(
            snaps,
            paid,
            at,
            self.cfg["settlement_grace_seconds"],
            self.client.declared_interval,
        )
        for c in final:
            self.store.put_comparison(c, at)
            self.store.put_record(
                "comparison", self.venue, record_key("comparison", c), c, at
            )
        self.store.set_checkpoint("pending:%s:%s" % (self.venue, mid), pending, at)

    def task_candles(self, at):
        self.ensure_markets(at)
        end = int(at) // 3600 * 3600  # candles opening before this hour are closed
        start = end - self.cfg["backfill_days"] * 86400
        new = failed = 0
        for m in self.selected:
            if self.stop.is_set():
                break
            mid = m["market_id"]
            name = "candle_attempts:%s:%s" % (self.venue, mid)
            attempts = self.store.checkpoint(name, {})
            missing = [
                t
                for t in range(start, end, 3600)
                if not self.store.has_record("candle", self.venue, "%s|%d" % (mid, t))
            ]
            retry = [
                t
                for t in missing
                if attempts.get(str(t), 0) < self.cfg["candle_retry_limit"]
            ]
            if not retry:
                continue
            try:
                rows, stats = self.client.candles(m, min(retry), end, at)
            except Exception as ex:
                failed += 1
                self.error("candles", mid, ex, at)
                continue
            self.bump(mid, {"candle_" + k: v for k, v in stats.items()})
            rows = [r for r in rows if start <= r["open_time"] < end]
            new += self.put("candle", rows, at)
            got = {int(r["open_time"]) for r in rows}
            for t in retry:
                if t not in got:
                    attempts[str(t)] = attempts.get(str(t), 0) + 1
            # Hours that left the window are forgotten.
            attempts = {k: v for k, v in attempts.items() if int(k) >= start}
            self.store.set_checkpoint(name, attempts, at)
        if failed and failed == len(self.selected):
            raise RuntimeError("Hiçbir piyasanın mumları okunamadı")
        return "%d yeni mum, %d piyasada hata" % (new, failed)

    def task_orderbook(self, at):
        self.ensure_markets(at)
        new = failed = 0
        for m in self.selected:
            if self.stop.is_set():
                break
            if (m.get("extra") or {}).get("isRfq"):
                continue  # an RFQ market's public book is indicative only
            try:
                book = self.client.orderbook(m, self.clock())
            except Exception as ex:
                failed += 1
                self.error("orderbook", m["market_id"], ex, at)
                continue
            new += self.put("orderbook", [book], at)
        if failed and failed == len(self.selected):
            raise RuntimeError("Hiçbir piyasanın defteri okunamadı")
        return "%d defter, %d piyasada hata" % (new, failed)

    # --- running -------------------------------------------------------------------
    def run_task(self, task):
        at = self.clock()
        try:
            note = getattr(self, "task_" + task)(at)
        except Exception as ex:
            self.error(task, None, ex, at)
            self.mark(task, False, at, "%s: %s" % (type(ex).__name__, str(ex)[:200]))
            return False
        self.mark(task, True, at, note)
        self.log.info("%s %s: %s", self.venue, task, note)
        return True

    def once(self):
        for task in TASKS:
            if self.stop.is_set():
                break
            self.run_task(task)

    def loop(self, until):
        while not self.stop.is_set() and self.clock() < until:
            now = self.clock()
            for task in TASKS:
                if self.stop.is_set():
                    break
                if now >= self.next_due[task]:
                    self.run_task(task)
                    self.next_due[task] = self.clock() + self.period[task]
            wait = min(self.next_due.values()) - self.clock()
            if wait > 0:
                self.stop.wait(min(wait, until - self.clock(), 30))


class Lock:
    """One recorder per folder: a lock file with a heartbeat; a stale one is taken over.
    (No process signals are used: on Windows they would end the process.)"""

    def __init__(self, root, clock=time.time):
        self.path = os.path.join(root, "recorder.lock")
        self.clock = clock

    def acquire(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                held = json.load(f)
            if self.clock() - held.get("heartbeat", 0) < LOCK_STALE:
                raise RuntimeError(
                    "Bu klasörde başka bir kayıt oturumu çalışıyor (PID %s)"
                    % held.get("pid")
                )
        except (OSError, ValueError):
            pass
        self.beat()

    def beat(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(pid=os.getpid(), heartbeat=self.clock()), f)
        os.replace(tmp, self.path)

    def release(self):
        try:
            os.remove(self.path)
        except OSError:
            pass


class Recorder:
    def __init__(self, cfg, transport=None, clock=time.time, sleep=None):
        self.cfg = cfg
        self.clock = clock
        self.store = Store(cfg["root"])
        # Where the data comes from: the real public APIs, or a test transport. One
        # folder never mixes the two, and every report says which it holds.
        self.source = (
            LIVE if transport is None else MOCK_PREFIX + type(transport).__name__
        )
        held = self.store.checkpoint("data_source")
        if held and (held == LIVE) != (self.source == LIVE):
            self.store.close()
            raise ValueError(
                "Bu klasörde %s verisi var; %s verisi aynı klasöre yazılmaz"
                % (held, self.source)
            )
        self.store.set_checkpoint("data_source", self.source)
        self.stop = threading.Event()
        self.log = setup_log(cfg["root"])
        self.venues = [
            VenueRecorder(
                make_client(v, self.store, cfg, transport, sleep, clock),
                self.store,
                cfg,
                self.stop,
                clock,
                self.log,
            )
            for v in cfg["venues"]
        ]
        self.stop_file = os.path.join(cfg["root"], "STOP")

    def run(self, mode="once", hours=None, poll=2.0):
        lock = Lock(self.cfg["root"], self.clock)
        lock.acquire()
        if os.path.exists(self.stop_file):
            os.remove(self.stop_file)
        started = self.clock()
        sid = self.store.start_session(mode, started, self.source)
        stopped_by = "bitti"
        until = started + (hours or 0) * 3600
        threads = [
            threading.Thread(
                target=(v.once if mode == "once" else lambda v=v: v.loop(until)),
                name="phase1-" + v.venue,
                daemon=True,
            )
            for v in self.venues
        ]
        self.log.info("Oturum başladı: %s %s", mode, iso(started))
        for t in threads:
            t.start()
        try:
            beat = self.clock()
            while any(t.is_alive() for t in threads):
                for t in threads:
                    t.join(poll / max(1, len(threads)))
                if os.path.exists(self.stop_file):
                    stopped_by = "STOP dosyası"
                    self.stop.set()
                if self.clock() - beat > 60:
                    lock.beat()
                    beat = self.clock()
        except KeyboardInterrupt:
            stopped_by = "Ctrl+C"
            self.stop.set()
            for t in threads:
                t.join(30)
        finally:
            self.stop.set()
            self.store.end_session(sid, self.clock(), stopped_by)
            lock.release()
            self.log.info("Oturum bitti: %s", stopped_by)
        return stopped_by

    def statuses(self):
        return [v.status for v in self.venues]


def setup_log(root):
    log = logging.getLogger("phase1")
    if not log.handlers:
        log.setLevel(logging.INFO)
        path = os.path.join(root, "logs", "recorder.log")
        h = logging.FileHandler(path, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
    return log
