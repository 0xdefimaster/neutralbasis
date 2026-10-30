"""The recorder's own files: raw responses, normalized fixtures, its index and its
checkpoints. Nothing here ever touches the paper engine's database.

    <root>/recorder.sqlite3     index: requests, raw files, fixture keys, checkpoints,
                                pending published/settled comparisons
    <root>/raw/<venue>/<day>/<sha256>.json.gz     each response body, content-addressed
    <root>/normalized/<kind>/<venue>/<day>.jsonl  one normalized record per line
    <root>/logs, reports, manifests, exports

Writes are idempotent: a raw body is stored once per venue and hash, a fixture once
per key.
All times are UTC epoch seconds.
"""

import gzip
import hashlib
import json
import os
import sqlite3
import threading
import time

VENUE_DIRS = dict(
    Hyperliquid="hyperliquid",
    LighterCore="lighter_core",
    Lighter="lighter_rh",
    Extended="extended",
    Variational="variational",
)
# The paper engine's data folders and database; the recorder refuses them.
ENGINE_DIRS = ("data-five-rh", "data", "data-railway")
ENGINE_DB = "paper.sqlite3"
SUBDIRS = ("raw", "normalized", "manifests", "logs", "reports", "exports")


def day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def iso(ts):
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def check_root(root):
    """Refuse a folder that is, or sits inside, one of the engine's data folders."""
    parts = [p.lower() for p in os.path.normpath(os.path.abspath(root)).split(os.sep)]
    for d in ENGINE_DIRS:
        if d in parts:
            raise ValueError(
                "Kayıt klasörü motorun veri klasörü olamaz: %s" % os.path.abspath(root)
            )
    return os.path.abspath(root)


class Store:
    def __init__(self, root):
        self.root = check_root(root)
        for d in SUBDIRS:
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
        path = os.path.join(self.root, "recorder.sqlite3")
        if os.path.basename(path) == ENGINE_DB:
            raise ValueError("Motor veritabanına yazılmaz")
        self.lock = threading.RLock()
        self.read_problems = set()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("pragma journal_mode=wal")
        self.db.executescript("""
            create table if not exists audit(id integer primary key, venue text,
              endpoint text, method text, url text, params text, body text,
              request_at real, response_at real, status integer, duration_ms real,
              sha256 text, size integer, headers text, error text, attempt integer,
              rate_limited integer);
            create table if not exists raw(sha256 text, venue text,
              path text, size integer, first_at real, primary key(sha256, venue));
            create table if not exists records(kind text, venue text, key text,
              raw_sha256 text, file text, at real, primary key(kind, venue, key));
            create table if not exists checkpoints(name text primary key, value text,
              at real);
            create table if not exists sessions(id integer primary key, started real,
              ended real, mode text, stopped_by text);
            create table if not exists settlement_index(venue text not null,
              market_id text not null, settled_at integer not null, revision integer,
              settled_values text, record_key text, identity_status text,
              primary key(venue, market_id, settled_at));
            """)
        self.db.commit()
        cols = [r[1] for r in self.db.execute("pragma table_info(settlement_index)")]
        if "identity_status" not in cols:
            self.db.execute(
                "alter table settlement_index add column identity_status text"
            )
            self.db.commit()
        cols = [r[1] for r in self.db.execute("pragma table_info(sessions)")]
        if "source" not in cols:
            self.db.execute("alter table sessions add column source text")
            self.db.commit()
        self.settlement_repair = self.ensure_settlement_index()
        self.repair = self.ensure_comparisons()

    def close(self):
        with self.lock:
            self.db.commit()
            self.db.close()

    # --- raw responses ---------------------------------------------------------------
    def put_raw(self, venue, body, at):
        digest = sha256(body)
        with self.lock:
            if self.db.execute(
                "select 1 from raw where sha256=? and venue=?", (digest, venue)
            ).fetchone():
                return digest
            rel = os.path.join("raw", VENUE_DIRS.get(venue, venue.lower()), day(at))
            os.makedirs(os.path.join(self.root, rel), exist_ok=True)
            rel = os.path.join(rel, digest + ".json.gz")
            tmp = os.path.join(self.root, rel + ".tmp")
            with gzip.open(tmp, "wb") as f:
                f.write(body)
            os.replace(tmp, os.path.join(self.root, rel))
            self.db.execute(
                "insert into raw values(?,?,?,?,?)",
                (digest, venue, rel.replace(os.sep, "/"), len(body), at),
            )
            self.db.commit()
        return digest

    def read_raw(self, digest):
        with self.lock:
            r = self.db.execute(
                "select path from raw where sha256=? limit 1", (digest,)
            ).fetchone()
        if not r:
            raise KeyError(digest)
        with gzip.open(os.path.join(self.root, r[0]), "rb") as f:
            return f.read()

    def audit(self, row):
        cols = list(row)
        with self.lock:
            self.db.execute(
                "insert into audit(%s) values(%s)"
                % (",".join(cols), ",".join("?" * len(cols))),
                [row[c] for c in cols],
            )
            self.db.commit()

    # --- normalized fixtures --------------------------------------------------------
    def put_record(self, kind, venue, key, record, at):
        """Append a normalized record once per (kind, venue, key); True if new."""
        rel = os.path.join(
            "normalized", kind, VENUE_DIRS.get(venue, venue.lower()), day(at) + ".jsonl"
        )
        with self.lock:
            cur = self.db.execute(
                "insert or ignore into records values(?,?,?,?,?,?)",
                (
                    kind,
                    venue,
                    key,
                    record.get("raw_sha256"),
                    rel.replace(os.sep, "/"),
                    at,
                ),
            )
            if cur.rowcount == 0:
                return False
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            line = json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
            # A line cut off by a crash must not swallow the next record.
            if os.path.exists(path) and os.path.getsize(path):
                with open(path, "rb") as f:
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        line = "\n" + line
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            # The index row commits only after the line is written: a crash in between
            # leaves a line without its row; the record is then written again, and
            # readers keep one record per key.
            self.db.commit()
        return True

    def has_record(self, kind, venue, key):
        with self.lock:
            return bool(
                self.db.execute(
                    "select 1 from records where kind=? and venue=? and key=?",
                    (kind, venue, key),
                ).fetchone()
            )

    def records(self, kind, venue=None, since=None):
        """Every stored normalized record of a kind (read back from the JSONL files);
        with `since`, only the day files from that UTC day on."""
        out = []
        first = day(since) + ".jsonl" if since is not None else None
        base = os.path.join(self.root, "normalized", kind)
        if not os.path.isdir(base):
            return out
        dirs = [VENUE_DIRS.get(venue, venue)] if venue else sorted(os.listdir(base))
        for d in dirs:
            folder = os.path.join(base, d)
            if not os.path.isdir(folder):
                continue
            for name in sorted(os.listdir(folder)):
                if name.endswith(".jsonl") and (first is None or name >= first):
                    with open(os.path.join(folder, name), encoding="utf-8") as f:
                        for i, line in enumerate(f, 1):
                            if not line.strip():
                                continue
                            try:
                                out.append(json.loads(line))
                            except ValueError:
                                # A line cut off by a crash: reported, never guessed.
                                self.read_problems.add((kind, d, name, i))
        return self._one_per_key(kind, out)

    @staticmethod
    def _one_per_key(kind, rows):
        from .clients.records import KINDS, record_key

        if kind not in KINDS:
            return rows
        seen = {}
        for r in rows:
            seen[(r.get("venue"), record_key(kind, r))] = r
        return list(seen.values())

    def latest(self, kind, venue=None, since=None):
        """Settlements or comparisons: the highest revision of each payment."""
        best = {}
        for r in self.records(kind, venue, since):
            k = (r["venue"], str(r["market_id"]), int(round(r["settled_at"])))
            rev = r.get("revision", r.get("settlement_revision")) or 1
            if k not in best or rev >= best[k][0]:
                best[k] = (rev, r)
        return [r for _, r in best.values()]

    # --- checkpoints ----------------------------------------------------------------
    def checkpoint(self, name, default=None):
        with self.lock:
            r = self.db.execute(
                "select value from checkpoints where name=?", (name,)
            ).fetchone()
        return json.loads(r[0]) if r else default

    def set_checkpoint(self, name, value, at=None):
        with self.lock:
            self.db.execute(
                "insert or replace into checkpoints values(?,?,?)",
                (name, json.dumps(value), at or time.time()),
            )
            self.db.commit()

    # --- comparisons ----------------------------------------------------------------
    # A completed comparison is identified by the payment it was matched with: venue,
    # market and the payment's actual settlement time (whole seconds, never null). The
    # venue's announced settlement time, when it publishes one, is only a field.
    FINAL = ("MATCHED", "SIGN_CONFLICT", "TOO_SMALL", "NO_SIGNED_RATE")
    # Stored but never counted: the payment behind it became AMBIGUOUS.
    EXCLUDED = ("EXCLUDED_AMBIGUOUS",)
    SCHEMA = (
        "create table comparisons(venue text not null, market_id text not null,"
        " settled_at integer not null, asset text, status text,"
        " expected_settlement_at real, body text, at real,"
        " primary key(venue, market_id, settled_at))"
    )

    def ensure_comparisons(self):
        """Create the comparison index, or rebuild it when it does not hold exactly the
        normalized comparison records: an index written before the identity fix (keyed
        on the announced time, null for Hyperliquid and Lighter, so one comparison
        could be stored many times), or one left behind by a crash. The rebuild runs
        in one transaction, so an interrupted one leaves the previous state and runs
        again on the next open. An old-schema table is renamed and kept, never
        dropped; raw responses and every other record are untouched. Returns a
        description of the repair, or None."""
        wanted = {}
        for c in self.latest("comparison"):
            if c.get("settled_at") is not None and c["comparison_status"] in (
                self.FINAL + self.EXCLUDED
            ):
                wanted[self._comparison_key(c)] = c
        with self.lock:
            cols = [r[1] for r in self.db.execute("pragma table_info(comparisons)")]
            current = "expected_settlement_at" in cols
            if not cols:
                self.db.execute(self.SCHEMA)
                self.db.commit()
            elif current:
                have = {}
                for v, m, t, body in self.db.execute(
                    "select venue, market_id, settled_at, body from comparisons"
                ):
                    have[(v, m, t)] = json.loads(body).get("settlement_revision") or 1
                rows = self.db.execute("select count(*) from comparisons").fetchone()[0]
                want = {k: c.get("settlement_revision") or 1 for k, c in wanted.items()}
                if rows == len(have) and have == want:
                    return None
            if not cols and not wanted:
                return None
            before = (
                self.db.execute("select count(*) from comparisons").fetchone()[0]
                if cols
                else 0
            )
            legacy = None
            self.db.commit()
            try:
                self.db.execute("begin")
                if cols and not current:
                    legacy = "comparisons_legacy_%d" % int(time.time())
                    n = 1
                    while self.db.execute(
                        "select 1 from sqlite_master where name=?", (legacy,)
                    ).fetchone():
                        n += 1
                        legacy = "comparisons_legacy_%d_%d" % (int(time.time()), n)
                    self.db.execute("alter table comparisons rename to %s" % legacy)
                    self.db.execute(self.SCHEMA)
                else:
                    self.db.execute("delete from comparisons")
                self.db.executemany(
                    "insert into comparisons values(?,?,?,?,?,?,?,?)",
                    [
                        self._comparison_row(c, c.get("observed_at"))
                        for c in wanted.values()
                    ],
                )
                note = dict(
                    at=time.time(),
                    legacy_table=legacy,
                    legacy_rows=before,
                    rebuilt_rows=len(wanted),
                    reason=(
                        "eski anahtar"
                        if cols and not current
                        else "dizin JSONL kayıtlarıyla uyuşmuyordu"
                    ),
                )
                self.db.execute(
                    "insert or replace into checkpoints values(?,?,?)",
                    ("repair:comparisons", json.dumps(note), note["at"]),
                )
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
        return note

    def rebuild_comparisons(self):
        """Rebuild the comparison index from the normalized records (see above)."""
        note = self.ensure_comparisons()
        return note["rebuilt_rows"] if note else self.comparison_counts()[0]

    @staticmethod
    def _comparison_key(c):
        return (c["venue"], str(c["market_id"]), int(round(c["settled_at"])))

    def _comparison_row(self, c, at):
        return (
            c["venue"],
            str(c["market_id"]),
            int(round(c["settled_at"])),
            c["asset"],
            c["comparison_status"],
            c.get("expected_settlement_at"),
            json.dumps(c, sort_keys=True),
            at,
        )

    def put_comparison(self, c, at):
        """Store a completed comparison once per (venue, market, actual settlement
        time); True when it is new. A comparison of a corrected payment (a higher
        settlement revision) replaces the earlier one. A pending one is refused."""
        if c.get("settled_at") is None or c.get("comparison_status") not in (
            self.FINAL + self.EXCLUDED
        ):
            raise ValueError("Yalnız tamamlanmış karşılaştırma kaydedilir")
        with self.lock:
            old = self.db.execute(
                "select body from comparisons where venue=? and market_id=?"
                " and settled_at=?",
                self._comparison_key(c),
            ).fetchone()
            if old is not None:
                prev = json.loads(old[0]).get("settlement_revision") or 1
                if (c.get("settlement_revision") or 1) <= prev:
                    return False
                self.db.execute(
                    "delete from comparisons where venue=? and market_id=?"
                    " and settled_at=?",
                    self._comparison_key(c),
                )
            self.db.execute(
                "insert into comparisons values(?,?,?,?,?,?,?,?)",
                self._comparison_row(c, at),
            )
            self.db.commit()
            return True

    # --- settlements: one record per payment, corrections as revisions --------------
    def ensure_settlement_index(self):
        """Keep the per-payment index equal to the normalized settlement records (a
        crash between the two, or a folder from before the index, is repaired in one
        transaction). Returns a description of the repair, or None."""
        latest = self.latest("settlement")
        with self.lock:
            have = {
                (v, m, t): r
                for v, m, t, r in self.db.execute(
                    "select venue, market_id, settled_at, revision from settlement_index"
                )
            }
            want = {
                (r["venue"], str(r["market_id"]), int(round(r["settled_at"]))): r.get(
                    "revision", 1
                )
                for r in latest
            }
            if have == want:
                return None
            self.db.commit()
            try:
                self.db.execute("begin")
                self.db.execute("delete from settlement_index")
                self.db.executemany(
                    "insert into settlement_index values(?,?,?,?,?,?,?)",
                    [self._settlement_row(r) for r in latest],
                )
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
        return dict(before=len(have), after=len(want))

    @staticmethod
    def _values(r):
        from .clients.records import SETTLED_FIELDS

        return json.dumps(
            [r.get(f) for f in SETTLED_FIELDS]
            + [r.get("settled_rate_status") == "CONFLICT"]
        )

    def _settlement_row(self, r):
        from .clients.records import record_key

        return (
            r["venue"],
            str(r["market_id"]),
            int(round(r["settled_at"])),
            r.get("revision", 1),
            self._values(r),
            record_key("settlement", r),
            r.get("identity_status", "UNIQUE"),
        )

    def put_settlement(self, r, at, tolerance=None):
        """Record one settled payment; returns what it was:
        new         a payment not seen before;
        same        already recorded with the same values (also when the venue
                    reported its time with a different sub-second part);
        correction  the venue now reports different values for a recorded payment
                    (at the same instant): a new revision with the previous values
                    beside it and a settlement_correction record; the first stays;
        ambiguous   a different payment a few seconds from a recorded one, which
                    cannot be told apart from it: both are kept and marked
                    AMBIGUOUS (the recorded one by a new revision), and neither is
                    counted or compared; an existing comparison is excluded.
        Only payments of the same venue and market are ever matched.
        """
        from .clients.records import (
            SAME_EVENT,
            SAME_INSTANT,
            SETTLED_FIELDS,
            record_key,
        )

        tol = SAME_EVENT if tolerance is None else tolerance
        t = int(round(r["settled_at"]))
        mid = str(r["market_id"])
        with self.lock:
            row = self.db.execute(
                "select settled_at, revision, settled_values, record_key,"
                " identity_status from settlement_index"
                " where venue=? and market_id=? and settled_at between ? and ?"
                " order by abs(settled_at - ?) limit 1",
                (r["venue"], mid, t - tol, t + tol, t),
            ).fetchone()
            if row is None:
                r = dict(r, settled_at=t, revision=1)
                self._index_settlement(r, insert=True)
                self.put_record(
                    "settlement", r["venue"], record_key("settlement", r), r, at
                )
                return "new"
            first_t, rev, values, key, identity = row
            if values == self._values(r):
                return "same"
            api = r.get("settled_at_api", t)
            if abs(api - first_t) > SAME_INSTANT + 0.5:
                return self._ambiguous(r, t, first_t, rev, identity, at)
            before = json.loads(values)
            r = dict(
                r,
                settled_at=first_t,
                revision=rev + 1,
                identity_status=identity or "UNIQUE",
                correction_of=key,
                previous=dict(zip(list(SETTLED_FIELDS) + ["conflict"], before)),
            )
            self._index_settlement(r)
            note = dict(
                venue=r["venue"],
                asset=r["asset"],
                market_id=r["market_id"],
                settled_at=first_t,
                revision=rev + 1,
                before=r["previous"],
                after={f: r.get(f) for f in SETTLED_FIELDS},
                raw_sha256=r["raw_sha256"],
                detected_at=at,
            )
            self.put_record(
                "settlement_correction",
                r["venue"],
                record_key("settlement_correction", note),
                note,
                at,
            )
            self.put_record(
                "settlement", r["venue"], record_key("settlement", r), r, at
            )
            return "correction"

    def _ambiguous(self, r, t, first_t, rev, identity, at):
        from .clients.records import record_key

        venue, mid = r["venue"], str(r["market_id"])
        new = dict(
            r,
            settled_at=t,
            revision=1,
            identity_status="AMBIGUOUS",
            ambiguous_with=first_t,
        )
        # More than a second from the recorded time, so its own whole second.
        self._index_settlement(new, insert=True)
        self.put_record("settlement", venue, record_key("settlement", new), new, at)
        if identity != "AMBIGUOUS":
            old = next(
                (
                    x
                    for x in self.latest("settlement", venue)
                    if str(x["market_id"]) == mid
                    and int(round(x["settled_at"])) == first_t
                ),
                None,
            )
            if old is not None:
                old = dict(
                    old,
                    revision=rev + 1,
                    identity_status="AMBIGUOUS",
                    ambiguous_with=t,
                    detected_at=at,
                )
                self._index_settlement(old)
                self.put_record(
                    "settlement", venue, record_key("settlement", old), old, at
                )
                self.exclude_comparison(venue, mid, first_t, rev + 1, at)
        return "ambiguous"

    def _index_settlement(self, r, insert=False):
        row = self._settlement_row(r)
        if insert:
            self.db.execute("insert into settlement_index values(?,?,?,?,?,?,?)", row)
        else:
            self.db.execute(
                "update settlement_index set revision=?, settled_values=?,"
                " record_key=?, identity_status=?"
                " where venue=? and market_id=? and settled_at=?",
                (row[3], row[4], row[5], row[6], row[0], row[1], row[2]),
            )

    def exclude_comparison(self, venue, mid, settled_at, revision, at):
        """A payment that became AMBIGUOUS no longer supports its comparison: a new
        revision of the comparison is stored as EXCLUDED_AMBIGUOUS (never counted)."""
        from .clients.records import record_key

        with self.lock:
            old = self.db.execute(
                "select body from comparisons where venue=? and market_id=?"
                " and settled_at=?",
                (venue, str(mid), int(settled_at)),
            ).fetchone()
        if old is None:
            return False
        c = dict(
            json.loads(old[0]),
            comparison_status="EXCLUDED_AMBIGUOUS",
            settlement_revision=revision,
            excluded_at=at,
        )
        self.put_comparison(c, at)
        self.put_record("comparison", venue, record_key("comparison", c), c, at)
        return True

    def observed_interval(self, venue, mid, last=50):
        """Median gap of the market's last recorded (non-ambiguous) payments, or None
        with fewer than six."""
        with self.lock:
            ts = [
                t
                for (t,) in self.db.execute(
                    "select settled_at from settlement_index where venue=? and"
                    " market_id=? and coalesce(identity_status, 'UNIQUE')"
                    " != 'AMBIGUOUS' order by settled_at desc limit ?",
                    (venue, str(mid), last),
                )
            ]
        ts.sort()
        gaps = sorted(b - a for a, b in zip(ts, ts[1:]) if b > a)
        if len(ts) < 6 or not gaps:
            return None
        return gaps[len(gaps) // 2]

    def index_rows(self, venue, mid, lo, hi):
        """[(settled_at, revision, identity_status)] of a market between two times."""
        with self.lock:
            return self.db.execute(
                "select settled_at, revision, identity_status from settlement_index"
                " where venue=? and market_id=? and settled_at between ? and ?"
                " order by settled_at",
                (venue, str(mid), int(lo), int(hi)),
            ).fetchall()

    def put_withdrawn(self, venue, asset, mid, settled_at, revision, at, window):
        """A recorded payment the venue no longer lists: reported, never deleted."""
        from .clients.records import record_key

        note = dict(
            venue=venue,
            asset=asset,
            market_id=mid,
            settled_at=int(settled_at),
            revision=revision,
            detected_at=at,
            window=window,
        )
        return self.put_record(
            "settlement_withdrawn",
            venue,
            record_key("settlement_withdrawn", note),
            note,
            at,
        )

    def comparisons(self, venue=None, status=None):
        q, args = "select body from comparisons where 1=1", []
        if venue:
            q, args = q + " and venue=?", args + [venue]
        if status:
            q, args = q + " and status=?", args + [status]
        with self.lock:
            return [
                json.loads(r[0])
                for r in self.db.execute(q + " order by settled_at", args)
            ]

    def comparison_counts(self):
        """(rows, distinct venue/market/settlement) of the comparison index."""
        with self.lock:
            return self.db.execute(
                "select count(*), (select count(*) from (select distinct venue,"
                " market_id, settled_at from comparisons)) from comparisons"
            ).fetchone()

    # --- sessions -------------------------------------------------------------------
    def start_session(self, mode, at, source=None):
        with self.lock:
            cur = self.db.execute(
                "insert into sessions(started, mode, source) values(?,?,?)",
                (at, mode, source or self.checkpoint("data_source")),
            )
            self.db.commit()
            return cur.lastrowid

    def end_session(self, sid, at, stopped_by):
        with self.lock:
            self.db.execute(
                "update sessions set ended=?, stopped_by=? where id=?",
                (at, stopped_by, sid),
            )
            self.db.commit()
