"""Disk retention for the paper desk's SQLite store.

Every public HTTP response is kept compressed in `audit` as evidence. The raw body is
only needed while a decision can still be checked against it, so after AUDIT_RAW_DAYS
the body is dropped and the row (time, URL, sha256 of the body) stays as a fingerprint
until AUDIT_KEEP_DAYS. Responses referenced by any stored position, fill or payment
keep their body. Marks and events are trimmed to what settlement and the panel read;
parsed candles and funding have their own 35-day window (history_store).
"""

import json, threading, time
import bot
from bot import now

AUDIT_RAW_DAYS = 3
AUDIT_KEEP_DAYS = 30
# Settlement looks up the last mark before a payment, up to a week after a close.
MARK_DAYS = 14
EVENT_DAYS = 30
# Rows and free pages handled per lock hold, and the time one background run may take,
# so position tracking never waits long on the shared store lock.
BATCH = 500
VACUUM_PAGES = 1024
RUN_SECONDS = 20
PAUSE = 0.02
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


def prune(store, at=None, deadline=None, pause=0):
    """Trim old rows in small batches, giving the lock back between them. With a
    deadline it stops early and reports done=False; the next run picks up the rest."""
    at = at or now()
    keep = referenced(store)
    out = dict(audit_stripped=0, audit_dropped=0, marks=0, events=0, done=True)

    def audit_pass(sql, change, key):
        last = 0
        while True:
            if deadline and time.monotonic() > deadline:
                return False
            with store.lock:
                rows = store.db.execute(sql, (last, BATCH)).fetchall()
                if not rows:
                    return True
                last = rows[-1][0]
                ids = [(i,) for (i,) in rows if i not in keep]
                store.db.executemany(change, ids)
                store.db.commit()
            out[key] += len(ids)
            time.sleep(pause)

    def rows_pass(table, cutoff, key):
        while True:
            if deadline and time.monotonic() > deadline:
                return False
            with store.lock:
                n = store.db.execute(
                    f"delete from {table} where rowid in "
                    f"(select rowid from {table} where ts<? limit ?)",
                    (cutoff, BATCH),
                ).rowcount
                store.db.commit()
            out[key] += n
            if n < BATCH:
                return True
            time.sleep(pause)

    raw = at - AUDIT_RAW_DAYS * 86400
    old = at - AUDIT_KEEP_DAYS * 86400
    out["done"] = (
        audit_pass(
            f"select id from audit where id>? and ts<{raw!r} and raw is not null"
            " order by id limit ?",
            "update audit set raw=null where id=?",
            "audit_stripped",
        )
        and audit_pass(
            f"select id from audit where id>? and ts<{old!r} order by id limit ?",
            "delete from audit where id=?",
            "audit_dropped",
        )
        and rows_pass("marks", at - MARK_DAYS * 86400, "marks")
        and rows_pass("events", at - EVENT_DAYS * 86400, "events")
    )
    return out


def incremental(store):
    with store.lock:
        return store.db.execute("pragma auto_vacuum").fetchone()[0] == 2


def enable_incremental_vacuum(store):
    """Switch the file to incremental auto-vacuum once. That needs one full VACUUM,
    which rewrites the whole file, so it only runs from maintain(), before the engine
    starts; never from the background thread."""
    if incremental(store):
        return False
    with store.lock:
        store.db.commit()
        store.db.execute("pragma auto_vacuum=incremental")
        store.db.execute("vacuum")
    return True


def release(store, deadline=None, pause=0):
    """Give free pages back in chunks; only possible once the file is incremental."""
    freed = 0
    if not incremental(store):
        return freed
    while not (deadline and time.monotonic() > deadline):
        with store.lock:
            free = store.db.execute("pragma freelist_count").fetchone()[0]
            if not free:
                break
            # The pragma frees one page per result row, so the rows must be read.
            store.db.execute(f"pragma incremental_vacuum({VACUUM_PAGES})").fetchall()
            store.db.commit()
        freed += min(free, VACUUM_PAGES)
        time.sleep(pause)
    with store.lock:
        # The WAL file keeps its high-water size until checkpointed.
        store.db.execute("pragma wal_checkpoint(truncate)")
    return freed


def size(store):
    with store.lock:
        pages = store.db.execute("pragma page_count").fetchone()[0]
        page = store.db.execute("pragma page_size").fetchone()[0]
    return pages * page


def maintain(path, log=print):
    """Offline step run before the engine opens the database: prune everything due and
    do the one-time full VACUUM that switches the file to incremental auto-vacuum."""
    store = bot.Store(path)
    try:
        if incremental(store):
            return False
        log("Veritabanı bakımı: eski kayıtlar temizleniyor ve dosya sıkıştırılıyor...")
        before = size(store)
        prune(store)
        enable_incremental_vacuum(store)
        release(store)
        log(f"Bakım bitti: {before / 1e6:.0f} MB -> {size(store) / 1e6:.0f} MB")
        return True
    finally:
        store.db.close()


class Retention:
    def __init__(self, store, stop):
        self.store = store
        self.stop = stop
        self.status = dict(status="idle")
        self.thread = None

    def run_once(self):
        deadline = time.monotonic() + RUN_SECONDS
        before = size(self.store)
        counts = prune(self.store, deadline=deadline, pause=PAUSE)
        freed = release(self.store, deadline=deadline, pause=PAUSE)
        self.status = dict(
            status="ok",
            ts=now(),
            bytes_before=before,
            bytes=size(self.store),
            # Without the offline VACUUM, deleted space is reused but not given back.
            vacuum_pending=not incremental(self.store),
            freed_pages=freed,
            **counts,
        )
        return self.status

    def start(self):
        if self.thread:
            return

        def loop():
            while not self.stop.is_set():
                wait = EVERY_SECONDS
                try:
                    if not self.run_once()["done"]:
                        wait = 60
                except Exception as ex:
                    self.status = dict(status="error", ts=now(), error=str(ex)[:200])
                self.stop.wait(wait)

        self.thread = threading.Thread(target=loop, daemon=True, name="retention")
        self.thread.start()
