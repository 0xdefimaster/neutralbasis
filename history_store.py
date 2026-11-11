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
# A worker with nothing due, or after an unexpected error, looks again after this long.
IDLE_SECONDS = 60
# Markets are re-listed after each batch so newly prioritized ones go next.
BATCH = 10
# A stored window that starts late (a failed or cut-short first fill) is retried
# backwards at this pace; venues that publish less history simply stay partial.
BACKFILL_SECONDS = 12 * 3600
# Candle holes refetched per market and retry, longest first.
MAX_SPANS = 6
# Venues whose docs publish no market-wide realized funding history (TxFlow: only the
# user-scoped userFundings, docs.txflow.com read 2026-11-11). Their funding is not
# fetched; that is a documented absence, not a failure, and their history is complete
# on candles alone. Nothing is filled in for the missing payments.
NO_PUBLIC_FUNDING = frozenset({"TxFlow"})


def missing_spans(stored, start, end):
    """Contiguous [from, to) runs of missing hours in [start, end), longest first."""
    spans, run = [], None
    for t in range(start, end, 3600):
        if t in stored:
            if run is not None:
                spans.append((run, t))
                run = None
        elif run is None:
            run = t
    if run is not None:
        spans.append((run, end))
    return sorted(spans, key=lambda s: s[0] - s[1])


def funding_gaps(times, start, interval):
    """[from, to) windows where at least one scheduled payment is missing: the head
    before the first stored payment and any spacing over 1.5 intervals."""
    if not times or not interval:
        return []
    times = sorted(times)
    gaps = []
    if times[0] - start > 2 * interval:
        gaps.append((start, times[0]))
    for x, y in zip(times, times[1:]):
        if y - x > 1.5 * interval:
            gaps.append((x + 1, y))
    return gaps


def complete(cov):
    """At least 95% of the 30-day window's candles and scheduled funding payments."""
    if cov["candles"] < cov["candle_hours"] * 0.95:
        return False
    expected = cov.get("funding_expected")
    if "funding" in cov and expected and cov["funding"] < expected * 0.95:
        return False
    return True


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

    def first_candle(self, v, a, kind="perp", start=0):
        with self.store.lock:
            r = self.store.db.execute(
                "select min(ts) from hist_candles where venue=? and asset=? and kind=? and ts>=?",
                (v, a, kind, start),
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

    def closes_at(self, ts, kind="perp"):
        """{(venue, asset): close} of every stored market's candle at hour `ts`."""
        with self.store.lock:
            rows = self.store.db.execute(
                "select venue,asset,price from hist_candles where kind=? and ts=?",
                (kind, int(ts)),
            ).fetchall()
        return {(v, a): p for v, a, p in rows}

    def last_funding(self, v, a):
        with self.store.lock:
            r = self.store.db.execute(
                "select max(ts) from hist_funding where venue=? and asset=?", (v, a)
            ).fetchone()
        return r[0] if r and r[0] is not None else None

    def first_funding(self, v, a, start=0):
        with self.store.lock:
            r = self.store.db.execute(
                "select min(ts) from hist_funding where venue=? and asset=? and ts>=?",
                (v, a, start),
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
    publish(v, a, kind, prices, rates, interval, price_type), and optionally group(v):
    venues sharing a group are filled by one worker (default: one worker per venue).
    """

    def __init__(self, store, source, stop, sleep=time.sleep):
        self.db = HistoryStore(store)
        self.source = source
        self.stop = stop
        self.sleep = sleep
        self.state = {}
        self.backfilled = {}
        self.thread = None
        self.hydration_thread = None
        self.workers = {}
        self.lock = threading.RLock()
        self.active = {}
        self.worker_errors = {}
        self.hydration = dict(status="pending", published=0)
        self.stored_count = None

    def publish_stored(self):
        """Make stored history visible right after a restart, before any request."""
        self.hydration = dict(status="loading", published=0)
        markets = self.db.markets()
        self.stored_count = len(markets)
        wants = getattr(self.source, "wants_published", lambda *key: True)
        for v, a, kind in markets:
            if self.stop.is_set():
                return
            # Non-selected markets are read on demand from SQLite. Reading all
            # their candles here, only for publish() to discard them, delayed all
            # venue workers by minutes on an existing database.
            if wants(v, a, kind):
                self.publish(v, a, kind, None, None)
                self.hydration["published"] += 1
        self.hydration["status"] = "ready"

    def publish(self, v, a, kind, interval, price_type):
        prices = self.db.candles(v, a, kind, hour(now()) - FIRST_DAYS * 86400 - 3600)
        rates = (
            self.db.funding(v, a, now() - FIRST_DAYS * 86400) if kind == "perp" else []
        )
        self.source.publish(v, a, kind, prices, rates, interval, price_type)

    def sync(self, v, a, kind, interval=None, price_type="trade"):
        """Append new candles and funding, then repair holes. Each part runs on its own:
        a failing candle call does not stop funding, and whatever was stored is
        published either way. Errors are raised together after publishing."""
        key = (v, a, kind)
        if self.db.last_candle(v, a, kind) is None:
            # A first fill already asks for the whole window; repairs are for later.
            self.backfilled[key] = now()
        added, errors = 0, []
        try:
            added += self.sync_candles(v, a, kind)
        except Exception as ex:
            errors.append("Mum: " + str(ex)[:160])
        if kind == "perp" and v not in NO_PUBLIC_FUNDING:
            try:
                added += self.sync_funding(v, a, interval)
            except Exception as ex:
                errors.append("Funding: " + str(ex)[:160])
        try:
            added += self.repair(v, a, kind, interval)
        except Exception as ex:
            errors.append("Onarım: " + str(ex)[:160])
        self.publish(v, a, kind, interval, price_type)
        if errors:
            raise ValueError("; ".join(errors))
        return added

    def sync_candles(self, v, a, kind):
        end = hour(now())
        last = self.db.last_candle(v, a, kind)
        start = max(end - FIRST_DAYS * 86400, (last + 3600) if last is not None else 0)
        if start >= end:
            return 0
        prices = self.source.candles(v, a, kind, start, end)
        return self.db.add_candles(
            v, a, kind, {t: p for t, p in prices.items() if start <= t < end}
        )

    def sync_funding(self, v, a, interval=None):
        lastf = self.db.last_funding(v, a)
        fstart = max(now() - FIRST_DAYS * 86400, (lastf + 1) if lastf else 0)
        if now() - fstart <= (interval or 3600) * 0.5:
            return 0
        return self.db.add_funding(v, a, self.source.funding(v, a, fstart))

    def repair(self, v, a, kind, interval=None):
        """Refetch holes in the 30-day window: the head before the first stored row and
        gaps between stored rows (an outage, a cut-short page). Retried at most every
        BACKFILL_SECONDS while holes remain; venues that never publish an hour (no
        trades) only cost one call per span per retry."""
        key = (v, a, kind)
        if now() - self.backfilled.get(key, -BACKFILL_SECONDS) < BACKFILL_SECONDS:
            return 0
        added = 0
        tried = False
        end = hour(now())
        floor = end - FIRST_DAYS * 86400
        stored = self.db.candles(v, a, kind, floor)
        if stored:
            for start, stop in missing_spans(stored, floor, max(stored) + 3600)[
                :MAX_SPANS
            ]:
                tried = True
                prices = self.source.candles(v, a, kind, start, stop)
                added += self.db.add_candles(
                    v, a, kind, {t: p for t, p in prices.items() if start <= t < stop}
                )
        if kind == "perp" and v not in NO_PUBLIC_FUNDING:
            ffloor = now() - FIRST_DAYS * 86400
            times = [r["ts"] for r in self.db.funding(v, a, ffloor)]
            gaps = funding_gaps(times, ffloor, interval)
            if gaps:
                tried = True
                rows = self.source.funding(v, a, gaps[0][0])
                added += self.db.add_funding(
                    v,
                    a,
                    [r for r in rows if any(s <= r["ts"] < e for s, e in gaps)],
                )
        if tried:
            self.backfilled[key] = now()
        return added

    def coverage(self, v, a, kind="perp", interval=None):
        """Stored rows against what a complete 30-day window holds."""
        end = hour(now())
        floor = end - FIRST_DAYS * 86400
        candles = self.db.candles(v, a, kind, floor)
        out = dict(
            candles=len(candles),
            candle_hours=FIRST_DAYS * 24,
            candles_from=min(candles) if candles else None,
        )
        rates = []
        funded = kind == "perp" and v not in NO_PUBLIC_FUNDING
        if kind == "perp" and not funded:
            out["funding_published"] = False
        if funded:
            rates = self.db.funding(v, a, now() - FIRST_DAYS * 86400)
            out.update(
                funding=len(rates),
                funding_expected=(
                    int(FIRST_DAYS * 86400 // interval) if interval else None
                ),
                funding_from=rates[0]["ts"] if rates else None,
            )
        # Keep the 30-day completeness metric, but also prove each supported
        # decision window. A configured 7-day strategy must not secretly require 30.
        out["windows"] = {}
        for days in range(7, FIRST_DAYS + 1):
            recent = sum(end - days * 86400 <= t < end for t in candles)
            enough = recent >= days * 24 * 0.95
            if funded:
                paid = sum(now() - days * 86400 < r["ts"] <= now() for r in rates)
                enough = enough and bool(interval) and paid >= int(days * 86400 / interval) * 0.95
            out["windows"][str(days)] = bool(enough)
        return out

    def due(self, key):
        s = self.state.get(key)
        if not s:
            return True
        wait = REFRESH_SECONDS if s["ok"] else RETRY_SECONDS
        return now() - s["ts"] >= wait

    def group(self, v):
        return getattr(self.source, "group", lambda v: v)(v)

    def run_once(self, limit=None, group=None):
        """One pass over the due markets (of one venue group, or all), in the source's
        priority order; returns how many were synced (at most `limit`)."""
        done = 0
        for v, a, kind, interval, price_type in self.source.markets():
            if self.stop.is_set() or (limit and done >= limit):
                break
            if group is not None and self.group(v) != group:
                continue
            key = (v, a, kind)
            if not self.due(key):
                continue
            worker = group or self.group(v)
            started = now()
            with self.lock:
                self.active[worker] = dict(venue=v, asset=a, kind=kind, started=started)
            try:
                added = self.sync(v, a, kind, interval, price_type)
                state = dict(ok=True, ts=now(), added=added)
            except Exception as ex:
                state = dict(ok=False, ts=now(), error=str(ex)[:400])
            try:
                cov = self.coverage(v, a, kind, interval)
                state.update(coverage=cov, complete=complete(cov))
            except Exception:
                pass
            with self.lock:
                state["seconds"] = now() - started
                self.state[key] = state
                self.active.pop(worker, None)
            done += 1
            self.sleep(PAUSE_SECONDS)
        return done

    def start(self):
        """Each venue group gets its own worker, so one slow or rate-limited venue no
        longer holds up the rest; new venues get a worker when they are first listed."""
        if self.thread:
            return

        def work(group):
            # An unexpected error waits a minute instead of ending the venue's worker.
            while not self.stop.is_set():
                try:
                    done = self.run_once(BATCH, group)
                except Exception as ex:
                    with self.lock:
                        self.worker_errors[group] = dict(ts=now(), error=str(ex)[:200])
                    done = 0
                if not done:
                    self.stop.wait(IDLE_SECONDS)

        def supervise():
            # Fresh data is useful before warm-cache restoration finishes.
            def hydrate():
                try:
                    self.publish_stored()
                except Exception as ex:
                    self.hydration = dict(status="error", error=str(ex)[:200])

            self.hydration_thread = threading.Thread(target=hydrate, daemon=True, name="history-cache")
            self.hydration_thread.start()
            while not self.stop.is_set():
                try:
                    groups = {self.group(m[0]) for m in self.source.markets()}
                except Exception as ex:
                    with self.lock:
                        self.worker_errors["supervisor"] = dict(ts=now(), error=str(ex)[:200])
                    groups = set()
                for g in sorted(groups - set(self.workers), key=str):
                    t = threading.Thread(
                        target=work, args=(g,), daemon=True, name="history-%s" % g
                    )
                    self.workers[g] = t
                    t.start()
                self.stop.wait(IDLE_SECONDS)

        self.thread = threading.Thread(target=supervise, daemon=True, name="history")
        self.thread.start()

    def market_state(self, v, a, kind="perp"):
        with self.lock:
            return dict(self.state.get((v, a, kind)) or {})

    def status(self):
        with self.lock:
            states = list(self.state.values())
            failing = [(k[0], s.get("error") or "") for k, s in self.state.items() if not s["ok"]]
            active = [dict(s, seconds=max(0, now()-s["started"])) for s in self.active.values()]
            errors = dict(self.worker_errors)
        # Failed markets grouped by venue and the start of their error, most common first:
        # what to fix, without listing every market.
        groups = {}
        for v, e in failing:
            g = groups.setdefault((v, str(e)[:120]), 0)
            groups[(v, str(e)[:120])] = g + 1
        failures = [
            dict(venue=v, error=e, markets=n)
            for (v, e), n in sorted(groups.items(), key=lambda x: -x[1])[:15]
        ]
        try:
            listed = len(self.source.markets())
        except Exception:
            listed = None
        try:
            needs = [tuple(k) for k in self.source.priority()]
        except Exception:
            needs = []
        with self.lock:
            missing = sum(not (self.state.get(k) or {}).get("complete") for k in needs)
        return dict(
            priority=len(needs),
            priority_missing=missing,
            markets=listed,
            synced=sum(s["ok"] for s in states),
            complete=sum(bool(s.get("complete")) for s in states),
            failed=sum(not s["ok"] for s in states),
            failures=failures,
            stored=self.stored_count,
            active=active,
            worker_errors=errors,
            hydration=dict(self.hydration),
            refresh_seconds=REFRESH_SECONDS,
        )
