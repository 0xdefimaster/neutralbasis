"""Persistent public price/funding history, filled in the background within rate limits.

Closed 1h candles and realized funding are kept in SQLite so a restart starts from
what is already stored and only new rows are fetched. Nothing is interpolated.
"""

import threading, time
from bot import now, hour, number

KEEP_DAYS = 35
FIRST_DAYS = 30
REFRESH_SECONDS = 1800
RETRY_SECONDS = 1800
PAUSE_SECONDS = 1.0
# Markets are re-listed after each batch so newly prioritized ones go next.
BATCH = 10


class HistoryStore:
    def __init__(self, store):
        self.store = store
        with store.lock:
            store.db.executescript(
                """create table if not exists hist_candles(venue text,asset text,kind text,ts integer,price real,primary key(venue,asset,kind,ts));
   create table if not exists hist_funding(venue text,asset text,ts real,rate real,value real,primary key(venue,asset,ts));"""
            )
            store.db.commit()

    def add_candles(self, v, a, kind, prices):
        rows = [(v, a, kind, int(t), number(p)) for t, p in prices.items() if p > 0]
        floor = hour(now()) - KEEP_DAYS * 86400
        with self.store.lock:
            self.store.db.executemany(
                "insert or replace into hist_candles values(?,?,?,?,?)", rows
            )
            self.store.db.execute(
                "delete from hist_candles where venue=? and asset=? and kind=? and ts<?",
                (v, a, kind, floor),
            )
            self.store.db.commit()
        return len(rows)

    def candles(self, v, a, kind="perp", start=0):
        with self.store.lock:
            rows = self.store.db.execute(
                "select ts,price from hist_candles where venue=? and asset=? and kind=? and ts>=? order by ts",
                (v, a, kind, start),
            ).fetchall()
        return {int(t): p for t, p in rows}

    def last_candle(self, v, a, kind="perp"):
        with self.store.lock:
            r = self.store.db.execute(
                "select max(ts) from hist_candles where venue=? and asset=? and kind=?",
                (v, a, kind),
            ).fetchone()
        return r[0] if r and r[0] is not None else None

    def add_funding(self, v, a, rows):
        data = [
            (v, a, float(r["ts"]), number(r["rate"]), r.get("value"))
            for r in rows
            if r.get("rate") is not None
        ]
        floor = now() - KEEP_DAYS * 86400
        with self.store.lock:
            self.store.db.executemany(
                "insert or replace into hist_funding values(?,?,?,?,?)", data
            )
            self.store.db.execute(
                "delete from hist_funding where venue=? and asset=? and ts<?",
                (v, a, floor),
            )
            self.store.db.commit()
        return len(data)

    def funding(self, v, a, start=0):
        with self.store.lock:
            rows = self.store.db.execute(
                "select ts,rate,value from hist_funding where venue=? and asset=? and ts>=? order by ts",
                (v, a, start),
            ).fetchall()
        return [dict(ts=t, rate=r, value=x, audit_id=None) for t, r, x in rows]

    def funding_sums(self, start):
        """{(venue, asset): (sum of rates, payment count)} for payments after `start`."""
        with self.store.lock:
            rows = self.store.db.execute(
                "select venue,asset,sum(rate),count(*) from hist_funding where ts>? group by venue,asset",
                (start,),
            ).fetchall()
        return {(v, a): (s, n) for v, a, s, n in rows}

    def last_funding(self, v, a):
        with self.store.lock:
            r = self.store.db.execute(
                "select max(ts) from hist_funding where venue=? and asset=?", (v, a)
            ).fetchone()
        return r[0] if r and r[0] is not None else None

    def markets(self):
        with self.store.lock:
            return self.store.db.execute(
                "select distinct venue,asset,kind from hist_candles"
            ).fetchall()


class HistoryKeeper:
    """Walks every listed market one at a time and appends only new closed rows.

    `source` provides: markets() -> [(venue, asset, kind, interval, price_type)],
    candles(v, a, kind, start, end) -> {ts: close}, funding(v, a, start) -> rows,
    publish(v, a, kind, prices, rates, interval, price_type).
    """

    def __init__(self, store, source, stop, sleep=time.sleep):
        self.db = HistoryStore(store)
        self.source = source
        self.stop = stop
        self.sleep = sleep
        self.state = {}
        self.thread = None
        self.lock = threading.RLock()

    def publish_stored(self):
        """Make stored history visible right after a restart, before any request."""
        for v, a, kind in self.db.markets():
            self.publish(v, a, kind, None, None)

    def publish(self, v, a, kind, interval, price_type):
        prices = self.db.candles(v, a, kind, hour(now()) - FIRST_DAYS * 86400 - 3600)
        rates = (
            self.db.funding(v, a, now() - FIRST_DAYS * 86400) if kind == "perp" else []
        )
        self.source.publish(v, a, kind, prices, rates, interval, price_type)

    def sync(self, v, a, kind, interval=None, price_type="trade"):
        end = hour(now())
        last = self.db.last_candle(v, a, kind)
        start = max(end - FIRST_DAYS * 86400, (last + 3600) if last is not None else 0)
        added = 0
        if start < end:
            prices = self.source.candles(v, a, kind, start, end)
            added += self.db.add_candles(
                v, a, kind, {t: p for t, p in prices.items() if start <= t < end}
            )
        if kind == "perp":
            lastf = self.db.last_funding(v, a)
            fstart = max(now() - FIRST_DAYS * 86400, (lastf + 1) if lastf else 0)
            if now() - fstart > (interval or 3600) * 0.5:
                added += self.db.add_funding(v, a, self.source.funding(v, a, fstart))
        self.publish(v, a, kind, interval, price_type)
        return added

    def due(self, key):
        s = self.state.get(key)
        if not s:
            return True
        wait = REFRESH_SECONDS if s["ok"] else RETRY_SECONDS
        return now() - s["ts"] >= wait

    def run_once(self, limit=None):
        """One pass over the due markets, in the source's priority order; returns how
        many were synced (at most `limit`)."""
        done = 0
        for v, a, kind, interval, price_type in self.source.markets():
            if self.stop.is_set() or (limit and done >= limit):
                break
            key = (v, a, kind)
            if not self.due(key):
                continue
            try:
                added = self.sync(v, a, kind, interval, price_type)
                with self.lock:
                    self.state[key] = dict(ok=True, ts=now(), added=added)
            except Exception as ex:
                with self.lock:
                    self.state[key] = dict(ok=False, ts=now(), error=str(ex)[:200])
            done += 1
            self.sleep(PAUSE_SECONDS)
        return done

    def start(self):
        if self.thread:
            return

        def loop():
            try:
                self.publish_stored()
            except Exception:
                pass
            while not self.stop.is_set():
                if not self.run_once(BATCH):
                    self.stop.wait(60)

        self.thread = threading.Thread(target=loop, daemon=True, name="history")
        self.thread.start()

    def status(self):
        with self.lock:
            states = list(self.state.values())
        try:
            listed = len(self.source.markets())
        except Exception:
            listed = None
        try:
            needs = [tuple(k) for k in self.source.priority()]
        except Exception:
            needs = []
        with self.lock:
            missing = sum(not (self.state.get(k) or {}).get("ok") for k in needs)
        return dict(
            priority=len(needs),
            priority_missing=missing,
            markets=listed,
            synced=sum(s["ok"] for s in states),
            failed=sum(not s["ok"] for s in states),
            stored=len(self.db.markets()),
            refresh_seconds=REFRESH_SECONDS,
        )
