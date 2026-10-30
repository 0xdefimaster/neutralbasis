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
            """)
        self.db.commit()
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
            os.makedirs(os.path.dirname(os.path.join(self.root, rel)), exist_ok=True)
            with open(os.path.join(self.root, rel), "a", encoding="utf-8") as f:
                f.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
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
                        out += [json.loads(line) for line in f if line.strip()]
        return out

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
    SCHEMA = (
        "create table comparisons(venue text not null, market_id text not null,"
        " settled_at integer not null, asset text, status text,"
        " expected_settlement_at real, body text, at real,"
        " primary key(venue, market_id, settled_at))"
    )

    def ensure_comparisons(self):
        """Create the comparison index, or repair one written before the identity fix
        (keyed on the announced time, which is null for Hyperliquid and Lighter, so
        one comparison could be stored many times). The old table is renamed, never
        dropped; the index is rebuilt from the normalized comparison records, which
        were always keyed by the actual settlement time. Nothing else is touched.
        Returns a description of the repair, or None."""
        with self.lock:
            cols = [r[1] for r in self.db.execute("pragma table_info(comparisons)")]
            if not cols:
                self.db.execute(self.SCHEMA)
                self.db.commit()
                return None
            current = "expected_settlement_at" in cols
            if current:
                total, distinct = self.db.execute(
                    "select count(*), (select count(*) from (select distinct venue,"
                    " market_id, settled_at from comparisons)) from comparisons"
                ).fetchone()
                if total == distinct:
                    return None
            before = self.db.execute("select count(*) from comparisons").fetchone()[0]
            legacy = "comparisons_legacy_%d" % int(time.time())
            self.db.execute("alter table comparisons rename to %s" % legacy)
            self.db.execute(self.SCHEMA)
            self.db.commit()
        rebuilt = self.rebuild_comparisons()
        note = dict(
            at=time.time(),
            legacy_table=legacy,
            legacy_rows=before,
            rebuilt_rows=rebuilt,
            reason="eski anahtar" if not current else "yinelenen satır",
        )
        self.set_checkpoint("repair:comparisons", note)
        return note

    def rebuild_comparisons(self):
        """Refill the comparison index from the normalized comparison records."""
        n = 0
        for c in self.records("comparison"):
            n += self.put_comparison(c, c.get("observed_at") or time.time())
        return n

    def put_comparison(self, c, at):
        """Store a completed comparison once per (venue, market, actual settlement
        time); True when it is new. A pending one has no settlement: refused."""
        if c.get("settled_at") is None or c.get("comparison_status") not in self.FINAL:
            raise ValueError("Yalnız tamamlanmış karşılaştırma kaydedilir")
        with self.lock:
            cur = self.db.execute(
                "insert or ignore into comparisons values(?,?,?,?,?,?,?,?)",
                (
                    c["venue"],
                    str(c["market_id"]),
                    int(round(c["settled_at"])),
                    c["asset"],
                    c["comparison_status"],
                    c.get("expected_settlement_at"),
                    json.dumps(c, sort_keys=True),
                    at,
                ),
            )
            self.db.commit()
            return cur.rowcount == 1

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
    def start_session(self, mode, at):
        with self.lock:
            cur = self.db.execute(
                "insert into sessions(started, mode) values(?,?)", (at, mode)
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
