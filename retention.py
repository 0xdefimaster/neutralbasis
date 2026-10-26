"""Disk retention for the paper desk's SQLite store.

Every public HTTP response is kept compressed in `audit` as evidence. The raw body is
only needed while a decision can still be checked against it, so after AUDIT_RAW_DAYS
the body is dropped and the row (time, URL, sha256 of the body) stays as a fingerprint
until AUDIT_KEEP_DAYS. Responses referenced by any stored position, fill or payment
keep their body. Marks and events are trimmed to what settlement and the panel read;
parsed candles and funding have their own 35-day window (history_store).
"""

import json, threading, time
from bot import now

AUDIT_RAW_DAYS = 3
AUDIT_KEEP_DAYS = 30
# Settlement looks up the last mark before a payment, up to a week after a close.
MARK_DAYS = 14
EVENT_DAYS = 30
BATCH = 5000
EVERY_SECONDS = 3600


def audit_ids(value, out):
    """Collect integer ids stored under any key containing 'audit'."""
    if isinstance(value, dict):
        for k, v in value.items():
            if "audit" in str(k):
                for x in v if isinstance(v, list) else [v]:
                    if isinstance(x, int) and not isinstance(x, bool):
                        out.add(x)
            else:
                audit_ids(v, out)
    elif isinstance(value, list):
        for v in value:
            audit_ids(v, out)
    return out


def referenced(store):
    refs = set()
    with store.lock:
        rows = store.db.execute("select v from kv").fetchall()
        rows += store.db.execute("select body from payments").fetchall()
    for (v,) in rows:
        try:
            audit_ids(json.loads(v), refs)
        except (TypeError, ValueError):
            continue
    return refs


def prune(store, at=None):
    at = at or now()
    keep = referenced(store)
    # Walk ids upward in batches so other threads get the lock in between.
    stripped = 0
    last = 0
    while True:
        with store.lock:
            rows = store.db.execute(
                "select id from audit where id>? and ts<? and raw is not null order by id limit ?",
                (last, at - AUDIT_RAW_DAYS * 86400, BATCH),
            ).fetchall()
            if not rows:
                break
            last = rows[-1][0]
            ids = [(i,) for (i,) in rows if i not in keep]
            store.db.executemany("update audit set raw=null where id=?", ids)
            store.db.commit()
        stripped += len(ids)
    dropped = 0
    last = 0
    while True:
        with store.lock:
            rows = store.db.execute(
                "select id from audit where id>? and ts<? order by id limit ?",
                (last, at - AUDIT_KEEP_DAYS * 86400, BATCH),
            ).fetchall()
            if not rows:
                break
            last = rows[-1][0]
            ids = [(i,) for (i,) in rows if i not in keep]
            store.db.executemany("delete from audit where id=?", ids)
            store.db.commit()
        dropped += len(ids)
    with store.lock:
        marks = store.db.execute(
            "delete from marks where ts<?", (at - MARK_DAYS * 86400,)
        ).rowcount
        events = store.db.execute(
            "delete from events where ts<?", (at - EVENT_DAYS * 86400,)
        ).rowcount
        store.db.commit()
    return dict(
        audit_stripped=stripped, audit_dropped=dropped, marks=marks, events=events
    )


def enable_incremental_vacuum(store):
    """Switch the file to incremental auto-vacuum once (needs one full VACUUM), so later
    prunes give space back without rewriting the whole file."""
    with store.lock:
        mode = store.db.execute("pragma auto_vacuum").fetchone()[0]
        if mode == 2:
            return False
        store.db.commit()
        store.db.execute("pragma auto_vacuum=incremental")
        store.db.execute("vacuum")
        return True


def release(store):
    with store.lock:
        free = store.db.execute("pragma freelist_count").fetchone()[0]
        store.db.execute("pragma incremental_vacuum")
        store.db.commit()
        # The WAL file keeps its high-water size until checkpointed.
        store.db.execute("pragma wal_checkpoint(truncate)")
    return free


def size(store):
    with store.lock:
        pages = store.db.execute("pragma page_count").fetchone()[0]
        page = store.db.execute("pragma page_size").fetchone()[0]
    return pages * page


class Retention:
    def __init__(self, store, stop):
        self.store = store
        self.stop = stop
        self.status = dict(status="idle")
        self.thread = None

    def run_once(self):
        before = size(self.store)
        counts = prune(self.store)
        vacuumed = enable_incremental_vacuum(self.store)
        freed = release(self.store)
        self.status = dict(
            status="ok",
            ts=now(),
            bytes_before=before,
            bytes=size(self.store),
            vacuumed=vacuumed,
            freed_pages=freed,
            **counts,
        )
        return self.status

    def start(self):
        if self.thread:
            return

        def loop():
            while not self.stop.is_set():
                try:
                    self.run_once()
                except Exception as ex:
                    self.status = dict(status="error", ts=now(), error=str(ex)[:200])
                self.stop.wait(EVERY_SECONDS)

        self.thread = threading.Thread(target=loop, daemon=True, name="retention")
        self.thread.start()
