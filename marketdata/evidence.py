"""Read-only view of a Phase 1 recorder folder.

The recorder may be running while this reads: its database is opened read-only and
its JSONL files are only read, so nothing in the folder changes. A line cut off by a
crash is skipped. Whether the folder holds live public-API data or MOCK test data
comes from the recorder's own `data_source` checkpoint; nothing else decides it.
"""

import json
import os
import sqlite3
import time
import urllib.request

LIVE = "LIVE_PUBLIC_API"
DIRS = dict(
    Hyperliquid="hyperliquid",
    LighterCore="lighter_core",
    Lighter="lighter_rh",
    Extended="extended",
    Variational="variational",
)
# Which recorded kind proves a capability, and which fields must hold a value.
PROOF = dict(
    market_discovery=("market", ()),
    instrument_metadata=(
        "market",
        ("min_order_size", "amount_step", "price_step", "max_leverage"),
    ),
    trading_constraints=("market", ("min_order_size", "amount_step", "price_step")),
    mark_price=("funding_snapshot", ("mark_price",)),
    index_price=("funding_snapshot", ("index_price",)),
    funding_current=("funding_snapshot", ("published_rate",)),
    funding_history=("settlement", ()),
    funding_settlements=("settlement", ()),
    ohlcv=("candle", ()),
    orderbook_l2=("orderbook", ("valid",)),
    bid_ask=("orderbook", ("valid",)),
)
TIME_FIELDS = ("received_at", "observed_at")
FILES_READ = 3  # newest daily files read per kind and venue for field checks


def _connect_ro(path):
    uri = "file:" + urllib.request.pathname2url(os.path.abspath(path)) + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def read_lines(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass  # cut off by a crash; the recorder reports it
    return out


class Evidence:
    def __init__(self, root, now=None):
        self.root = os.path.abspath(root)
        self.now = now or time.time()
        self.source = None
        self.api = {}
        self.counts = {}  # (venue, kind) -> (records, last write time)
        self.error = None
        self._seen = {}
        db_path = os.path.join(self.root, "recorder.sqlite3")
        if not os.path.exists(db_path):
            self.error = "Kayıt klasörü bulunamadı: %s" % db_path
            return
        try:
            db = _connect_ro(db_path)
            try:
                self._read_db(db)
            finally:
                db.close()
        except sqlite3.Error as ex:
            self.error = "Kayıt veritabanı okunamadı: %s" % ex

    @property
    def live(self):
        return self.source == LIVE

    def _read_db(self, db):
        row = db.execute(
            "select value from checkpoints where name='data_source'"
        ).fetchone()
        self.source = json.loads(row[0]) if row else None
        hour = self.now - 3600
        for venue, n, ok, lok, lerr, recent in db.execute(
            "select venue, count(*), sum(error is null),"
            " max(case when error is null then response_at end),"
            " max(case when error is not null then response_at end),"
            " sum(error is not null and response_at >= ?)"
            " from audit group by venue",
            (hour,),
        ):
            self.api[venue] = dict(
                requests=n,
                ok=ok or 0,
                errors=n - (ok or 0),
                last_ok=lok,
                last_error=lerr,
                errors_last_hour=recent or 0,
            )
        for kind, venue, n, last in db.execute(
            "select kind, venue, count(*), max(at) from records group by kind, venue"
        ):
            self.counts[(venue, kind)] = (n, last)

    def files(self, kind, venue):
        d = os.path.join(self.root, "normalized", kind, DIRS.get(venue, venue.lower()))
        if not os.path.isdir(d):
            return []
        return [
            os.path.join(d, n) for n in sorted(os.listdir(d)) if n.endswith(".jsonl")
        ]

    def recent(self, kind, venue, files=FILES_READ):
        out = []
        for path in self.files(kind, venue)[-files:]:
            out += read_lines(path)
        return out

    def capability(self, venue, cap):
        """{last_at, count} when the folder holds a record proving `cap` for `venue`
        (a record of the proving kind with at least one of its fields filled), else
        None."""
        if venue is None or cap not in PROOF:
            return None
        key = (venue, cap)
        if key not in self._seen:
            kind, fields = PROOF[cap]
            n, last = self.counts.get((venue, kind), (0, None))
            found = None
            if n:
                if not fields:
                    found = dict(last_at=last, count=n)
                else:
                    times = [
                        next((r[f] for f in TIME_FIELDS if r.get(f) is not None), None)
                        for r in self.recent(kind, venue)
                        # Any listed field with a value proves it; `is`, not `in`:
                        # a published rate of 0.0 is a value.
                        if any(
                            r.get(f) is not None and r.get(f) is not False
                            for f in fields
                        )
                    ]
                    times = [t for t in times if t is not None]
                    if times:
                        found = dict(last_at=max(times), count=len(times))
            self._seen[key] = found
        return self._seen[key]

    def markets(self, venue):
        """Every recorded market record of a venue (all days), oldest first."""
        rows = []
        for path in self.files("market", venue):
            rows += read_lines(path)
        return sorted(rows, key=lambda r: r.get("observed_at") or 0)

    def latest_marks(self, venue):
        """{market_id: (mark, received_at)} from the newest funding snapshots."""
        out = {}
        for r in self.recent("funding_snapshot", venue, files=1):
            mark, at = r.get("mark_price"), r.get("received_at")
            if mark is None or at is None:
                continue
            key = str(r.get("market_id"))
            if key not in out or at > out[key][1]:
                out[key] = (mark, at)
        return out
