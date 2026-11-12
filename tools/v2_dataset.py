"""Freeze the 2026-11-10..12 PAPER pilot evidence into one replayable dataset.

PAPER / research only. Every source is opened READ-ONLY (sqlite URI mode=ro); nothing
is written to a pilot, observer or engine database. The output is a new SQLite file
plus a manifest (sha256 of the dataset, size and mtime of every source), so the
baseline, the benchmark and the observer comparison run on the same frozen inputs
and give the same numbers every time.

What is exported (each row keeps the pilot it came from):
  trades, events, ledger_events, payments, periods   paper (SIMULATED) records
  hist_funding                                       published funding payments
                                                     (OBSERVED, public market data)
  marks_window                                       observed marks around the
                                                     observer run (loose pilot)
  observations, observer_rounds                      OBSERVED_PUBLISHED observer rows

Usage: python -m tools.v2_dataset --out DIR [--strict DB] [--loose DB] [--observer DB]
"""
import argparse
import datetime
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

DEFAULTS = dict(
    strict=r"C:\Users\W10\nb-pilot\data\paper.sqlite3",
    loose=r"C:\Users\W10\nb-pilot-loose\data\paper.sqlite3",
    observer=r"C:\Users\W10\nb-observer\observed_funding.sqlite3",
)
KV_KEYS = ("wide_closed", "wide_positions", "wide_config", "wide_epochs", "wide_fees",
           "wide_run", "wide_workspace", "wide_wallet", "wide_ledger")
SCHEMA = """
create table meta(k text primary key, v text);
create table trades(pilot text, id text, period_id integer, strategy text, asset text,
  long text, short text, opened real, closed_at real, size real, price_pnl real,
  funding real, fees real, net real, reason text, pending integer, body text,
  primary key(pilot, id));
create table events(pilot text, id integer, ts real, kind text, body text,
  primary key(pilot, id));
create table ledger_events(pilot text, event_key text, ts real, kind text,
  position text, venue text, amount real, body text, primary key(pilot, event_key));
create table payments(pilot text, key text, ts real, body text, primary key(pilot, key));
create table periods(pilot text, id integer, started real, ended real, capital real,
  venues text, workspace text, config text, trades integer, net real, source text,
  primary key(pilot, id));
create table hist_funding(pilot text, venue text, asset text, ts real, rate real,
  primary key(pilot, venue, asset, ts));
create table marks_window(pilot text, venue text, asset text, ts real, price real);
create table observations(venue text, asset text, observed real, source_ts real,
  rate_raw real, rate_unit text, interval_s real, next_ts real, mark real,
  index_px real, bid_1k real, ask_1k real, label text,
  primary key(venue, asset, observed));
create table kv(pilot text, k text, v text, primary key(pilot, k));
create table observer_rounds(started real primary key, venue text, markets integer,
  errors integer, seconds real, note text);
"""


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % Path(path).as_posix(), uri=True)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def source_info(path):
    p = Path(path)
    info = dict(path=str(p), exists=p.exists())
    if p.exists():
        st = p.stat()
        info.update(size=st.st_size, mtime=st.st_mtime)
        wal = Path(str(p) + "-wal")
        info["wal_size"] = wal.stat().st_size if wal.exists() else None
    return info


def copy(src, dst, sql, table, pilot, n_cols):
    rows = src.execute(sql).fetchall()
    marks = ",".join("?" * (n_cols + (1 if pilot is not None else 0)))
    data = [((pilot,) + tuple(r)) if pilot is not None else tuple(r) for r in rows]
    dst.executemany("insert or ignore into %s values(%s)" % (table, marks), data)
    return len(data)


def export(out_dir, strict, loose, observer):
    out_dir = Path(out_dir)
    if str(out_dir.resolve()).lower().startswith("e:"):
        # Owner rule until a separate decision: no new evidence on the failing E: disk.
        raise SystemExit("dataset must not be written to E:")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "dataset.sqlite3"
    if path.exists():
        raise SystemExit("%s exists; a frozen dataset is never overwritten" % path)
    dst = sqlite3.connect(str(path))
    dst.executescript(SCHEMA)
    counts = {}
    obs = ro(observer)
    window = obs.execute("select min(observed), max(observed) from observations").fetchone()
    for pilot, db_path in (("STRICT", strict), ("LOOSE", loose)):
        src = ro(db_path)
        c = counts.setdefault(pilot, {})
        c["trades"] = copy(src, dst, "select * from paper_trades", "trades", pilot, 16)
        c["events"] = copy(src, dst, "select id, ts, kind, body from events", "events", pilot, 4)
        c["ledger_events"] = copy(
            src, dst,
            "select event_key, ts, kind, position, venue, amount, body from ledger_events",
            "ledger_events", pilot, 7)
        c["payments"] = copy(src, dst, "select key, ts, body from payments", "payments", pilot, 3)
        c["periods"] = copy(
            src, dst,
            "select id, started, ended, capital, venues, workspace, config, trades, net,"
            " source from paper_periods", "periods", pilot, 10)
        c["hist_funding"] = copy(
            src, dst, "select venue, asset, ts, rate from hist_funding",
            "hist_funding", pilot, 4)
        # Full positions (with the entry decision) of closed and open paper trades,
        # and the settings they ran under.
        c["kv"] = copy(
            src, dst,
            "select k, v from kv where k in (%s)" % ",".join("'%s'" % k for k in KV_KEYS),
            "kv", pilot, 2)
        if pilot == "LOOSE" and window[0]:
            c["marks_window"] = copy(
                src, dst,
                "select venue, asset, ts, price from marks where ts between %f and %f"
                % (window[0] - 600, window[1] + 600), "marks_window", pilot, 4)
        src.close()
    counts["OBSERVER"] = dict(
        observations=copy(obs, dst, "select * from observations", "observations", None, 13),
        rounds=copy(obs, dst, "select * from rounds", "observer_rounds", None, 6))
    obs.close()
    sources = dict(strict=source_info(strict), loose=source_info(loose),
                   observer=source_info(observer))
    created = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for k, v in (("created_utc", created), ("sources", json.dumps(sources)),
                 ("counts", json.dumps(counts)),
                 ("labels", "trades/events/ledger/payments/periods = SIMULATED paper "
                            "records; hist_funding/marks = OBSERVED public market data; "
                            "observations = OBSERVED_PUBLISHED")):
        dst.execute("insert into meta values(?,?)", (k, v))
    dst.commit()
    dst.close()
    manifest = dict(dataset=str(path), sha256=sha256(path), size=path.stat().st_size,
                    created_utc=created, sources=sources, counts=counts)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    for k, v in DEFAULTS.items():
        ap.add_argument("--" + k, default=v)
    a = ap.parse_args()
    m = export(a.out, a.strict, a.loose, a.observer)
    print(json.dumps(dict(sha256=m["sha256"], size=m["size"], counts=m["counts"]), indent=1))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    main()
