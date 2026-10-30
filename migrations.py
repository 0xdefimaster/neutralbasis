"""Versioned schema migrations for the paper store.

Each migration has a version, a name, an `up` and a `down`. Pending migrations run in
order when the store opens. Before each one the paper records (kv, payments, events
and the migration table) are copied to a backup file next to the database; the
migration then runs in one transaction and is recorded in `schema_migrations` only if
it completed. A failed migration leaves the database as it was. `rollback` runs the
`down` of every migration above a version, newest first.

Migrations only add: existing tables and their rows are never rewritten, so the old
JSON records stay readable as they are.
"""

import json, os, time

# Tables a backup copies: everything the paper desk's state lives in (the raw HTTP
# evidence in `audit` is large, append-only and never touched by a migration).
BACKUP_TABLES = ("kv", "payments", "events", "schema_migrations")


def ensure_table(db):
    db.execute(
        "create table if not exists schema_migrations("
        "version integer primary key, name text, applied_at real)"
    )


def current(db):
    ensure_table(db)
    r = db.execute("select max(version) from schema_migrations").fetchone()
    return r[0] or 0


def backup(db, path):
    """Copy the paper-record tables to a new SQLite file at `path`."""
    if os.path.exists(path):
        raise ValueError("Yedek dosyası zaten var: " + path)
    db.commit()
    db.execute("attach database ? as bak", (path,))
    try:
        for t in BACKUP_TABLES:
            exists = db.execute(
                "select 1 from main.sqlite_master where type='table' and name=?", (t,)
            ).fetchone()
            if exists:
                db.execute(f"create table bak.{t} as select * from main.{t}")
        db.commit()
    finally:
        db.execute("detach database bak")
    return path


def backup_path(db_path, version):
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    base = "%s.backup-v%d-%s" % (db_path, version, stamp)
    path, n = base + ".sqlite3", 1
    while os.path.exists(path):
        n += 1
        path = "%s-%d.sqlite3" % (base, n)
    return path


def apply(db, version, name, up, now):
    db.commit()
    db.execute("begin")
    try:
        up(db)
        db.execute("insert into schema_migrations values(?,?,?)", (version, name, now))
        db.commit()
    except Exception:
        db.rollback()
        raise


def migrate(store, db_path=None, migrations=None, now=time.time):
    """Run the pending migrations; returns [(version, backup file)] of those applied."""
    migrations = MIGRATIONS if migrations is None else migrations
    done = []
    with store.lock:
        have = current(store.db)
        has_records = store.db.execute("select count(*) from kv").fetchone()[0] > 0
        for version, name, up, _ in migrations:
            if version <= have:
                continue
            # A fresh database has nothing to lose; any other gets a backup first.
            path = (
                backup(store.db, backup_path(db_path, version))
                if db_path and has_records
                else None
            )
            apply(store.db, version, name, up, now())
            done.append((version, path))
    return done


def rollback(store, to_version, migrations=None):
    """Undo the migrations above `to_version`, newest first, each in a transaction."""
    migrations = MIGRATIONS if migrations is None else migrations
    with store.lock:
        db = store.db
        have = current(db)
        for version, name, _, down in sorted(migrations, reverse=True):
            if version <= to_version or version > have:
                continue
            db.commit()
            db.execute("begin")
            try:
                down(db)
                db.execute("delete from schema_migrations where version=?", (version,))
                db.commit()
            except Exception:
                db.rollback()
                raise


# --- 1: normalized paper records ----------------------------------------------------
def up_1(db):
    db.execute(
        "create table paper_periods(id integer primary key, started real, ended real,"
        " capital real, venues text, workspace text, config text, trades integer,"
        " net real, source text)"
    )
    db.execute(
        "create table paper_trades(id text primary key, period_id integer,"
        " strategy text, asset text, long text, short text, opened real,"
        " closed_at real, size real, price_pnl real, funding real, fees real,"
        " net real, reason text, pending integer, body text)"
    )
    db.execute(
        "create table ledger_events(id integer primary key, event_key text unique,"
        " ts real, kind text, position text, venue text, amount real, body text)"
    )
    db.execute("create index paper_trades_period on paper_trades(period_id)")
    db.execute("create index ledger_events_position on ledger_events(position)")
    backfill_1(db)


def down_1(db):
    for t in ("ledger_events", "paper_trades", "paper_periods"):
        db.execute(f"drop table if exists {t}")


def _kv(db, key, default):
    r = db.execute("select v from kv where k=?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def backfill_1(db):
    """Mirror the existing JSON records: archived periods with their kept trades, the
    running period with its ledger, and every recorded funding payment."""
    from ledger import trade_row, period_values, payment_event, trade_events

    for e in _kv(db, "wide_epochs", []):
        cur = db.execute(
            "insert into paper_periods(started, ended, capital, venues, workspace,"
            " config, trades, net, source) values(?,?,?,?,?,?,?,?,?)",
            period_values(e, "arşiv"),
        )
        pid = cur.lastrowid
        for p in e.get("closed") or []:
            if p.get("legs") and p.get("id"):
                db.execute(
                    "insert or ignore into paper_trades values"
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    trade_row(p, pid),
                )
                for row in trade_events(p):
                    db.execute(
                        "insert or ignore into ledger_events(event_key, ts, kind,"
                        " position, venue, amount, body) values(?,?,?,?,?,?,?)",
                        row,
                    )
    wallet = _kv(db, "wide_wallet", {})
    cur = db.execute(
        "insert into paper_periods(started, ended, capital, venues, workspace, config,"
        " trades, net, source) values(?,?,?,?,?,?,?,?,?)",
        (
            _kv(db, "wide_epoch_start", None),
            None,
            (_kv(db, "wide_config", {}) or {}).get("wide_capital"),
            json.dumps(wallet.get("venues")),
            json.dumps(_kv(db, "wide_workspace", None)),
            json.dumps(_kv(db, "wide_config", {})),
            None,
            None,
            "açık dönem",
        ),
    )
    pid = cur.lastrowid
    ledger = _kv(db, "wide_ledger", {})
    closed = [p for p in _kv(db, "wide_closed", []) if p.get("legs") and p.get("id")]
    for p in closed:
        db.execute(
            "insert or ignore into paper_trades values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            trade_row(p, pid),
        )
        for row in trade_events(p):
            db.execute(
                "insert or ignore into ledger_events(event_key, ts, kind, position,"
                " venue, amount, body) values(?,?,?,?,?,?,?)",
                row,
            )
    for tid, x in ledger.items():
        db.execute(
            "insert or ignore into paper_trades values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            trade_row(dict(x, id=tid, legs=None, ledger=True), pid),
        )
    for key, ts, body in db.execute("select key, ts, body from payments").fetchall():
        row = payment_event(key, ts, json.loads(body))
        db.execute(
            "insert or ignore into ledger_events(event_key, ts, kind, position, venue,"
            " amount, body) values(?,?,?,?,?,?,?)",
            row,
        )


MIGRATIONS = [(1, "normalized paper records", up_1, down_1)]
