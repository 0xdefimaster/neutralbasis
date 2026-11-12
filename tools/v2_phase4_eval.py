"""V2 phase 4 evaluation: every ranked candidate through the V2 gate and strategies.

PAPER / research only. Two steps:

--fetch (explicit opt-in, public reads under the V2 network guard): the V2 collector
reads the eight venues' metadata, then, for V2 strategy rows only, each leg's order book
and 7 days of settled payments. Every answer is saved (answers.json.gz + sha256).

evaluate (default, offline): the saved answers are replayed; candidates come from
wide_paper.rank (funding) and rank_spreads (convergence); each one, and the hybrid of
each pair, goes through v2.gates.Gate on the real books at wide_size. Hourly closes for
the price statistics come from the LOOSE pilot database (read-only, OBSERVED, last
close 2026-11-11 23:00); Variational has no price history anywhere.

Usage: python -m tools.v2_phase4_eval --dir DIR [--fetch] [--pilot-db PATH]
"""
import argparse
import collections
import concurrent.futures
import gzip
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import wide_paper  # noqa: E402
from v2 import gates  # noqa: E402
from v2.collector import Collector  # noqa: E402
from v2.venues import base  # noqa: E402

from v2 import paths as _paths  # noqa: E402

PILOT_DB = str(_paths.pilot_db())


def key(xid, method, url, body):
    return "%s %s %s %s" % (xid, method, url, json.dumps(body, sort_keys=True))


class Recording:
    """Live reads, recorded. With `previous`, answers already recorded are reused
    (no request) and only missing ones are fetched."""

    def __init__(self, previous=None):
        self.answers = dict(previous or {})

    def __call__(self, xid, method, url, body, headers=None):
        k = key(xid, method, url, body)
        if k in self.answers:
            return self.answers[k]
        obj = base.http_json(xid, method, url, body, headers)
        self.answers[key(xid, method, url, body)] = obj
        return obj


class Replay:
    def __init__(self, answers):
        self.answers = answers
        self.missing = collections.Counter()

    def __call__(self, xid, method, url, body, headers=None):
        k = key(xid, method, url, body)
        if k not in self.answers:
            self.missing[xid] += 1
            raise LookupError("not recorded: " + k[:160])
        return self.answers[k]


def fetch(d, more=False):
    from pilot import network_guard

    network_guard.install(str(d / "guard.jsonl"), v2=True)
    previous, at0 = {}, None
    if more and (d / "answers.json.gz").exists():
        blob = json.loads(gzip.decompress((d / "answers.json.gz").read_bytes()))
        previous, at0 = blob["answers"], blob["at"]
    rec = Recording(previous)
    col = Collector(transport=rec, clock=(lambda: at0) if at0 else time.time)
    rows = col.refresh()
    at = at0 or time.time()
    by_venue = collections.defaultdict(list)
    for r in rows:
        by_venue[r["exchange_id"]].append(r)

    def venue_reads(xid):
        n = 0
        for r in by_venue[xid]:
            for fn in (col.book, lambda v, a: col.settled(v, a, at)):
                try:
                    fn(r["venue"], r["asset"])
                    n += 1
                except Exception:
                    pass
        return xid, n

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        done = dict(pool.map(venue_reads, list(by_venue)))
    raw = json.dumps(dict(at=at, answers=rec.answers)).encode()
    (d / "answers.json.gz").write_bytes(gzip.compress(raw))
    (d / "manifest.json").write_text(json.dumps(dict(
        created=at, sha256=hashlib.sha256(raw).hexdigest(), requests=len(rec.answers),
        reads_per_venue=done, label="OBSERVED public market data (V2 phase 4)"), indent=1),
        encoding="utf-8")
    print("saved", len(rec.answers), "answers", done)


def pilot_closes(path):
    db = sqlite3.connect("file:%s?mode=ro" % Path(path).as_posix(), uri=True)
    end = db.execute("select max(ts) from hist_candles").fetchone()[0]
    out = collections.defaultdict(dict)
    for v, a, ts, p in db.execute(
            "select venue, asset, ts, price from hist_candles where kind='perp' and ts>=?",
            (end - 8 * 86400,)):
        out[(v, a)][ts] = p
    db.close()
    return out, end


class Provider:
    def __init__(self, col, closes, session=None):
        self.col, self.closes_, self.session = col, closes, session

    def rows_by(self):
        return dict(self.col.by_key)

    def book(self, v, a):
        try:
            return self.col.book(v, a)
        except Exception:
            return None

    def settled(self, v, a, at):
        try:
            return self.col.settled(v, a, at)
        except Exception:
            return None

    def closes(self, v, a):
        return self.closes_.get((v, a))

    def rth_open(self):
        # session: None = as published; True = counterfactual "session open".
        return True if self.session else self.col.rth_open()


def evaluate(d, pilot_db, assume_open=False):
    blob = json.loads(gzip.decompress((d / "answers.json.gz").read_bytes()))
    at = blob["at"]
    replay = Replay(blob["answers"])
    col = Collector(transport=replay, clock=lambda: at)
    rows = col.refresh()
    closes, closes_end = pilot_closes(pilot_db)
    cfg = dict(wide_paper.WIDE_DEFAULTS)
    gate = gates.Gate(Provider(col, closes, assume_open), cfg)
    funding_c = wide_paper.rank(rows, cfg, at)
    conv_c = wide_paper.rank_spreads(rows, at, cfg)
    results = []
    for c in funding_c:
        results.append(("funding", c, gate.evaluate(dict(c, strategy="funding"), at,
                                                    "funding")))
    # Convergence: both directions of every ranked pair (the gap may sit far above or
    # far below its normal level).
    for c in conv_c:
        for lv, sv in ((c["long"], c["short"]), (c["short"], c["long"])):
            cc = dict(asset=c["asset"], long=lv, short=sv, strategy="convergence")
            results.append(("convergence", cc, gate.evaluate(cc, at, "convergence")))
    pairs = {(c["asset"], c["long"], c["short"]) for _, c, _ in results}
    for asset, lv, sv in sorted(pairs):
        c = dict(asset=asset, long=lv, short=sv)
        results.append(("hybrid", c, gate.evaluate(c, at, "hybrid")))
    summary = {}
    for strategy in ("funding", "convergence", "hybrid"):
        evs = [ev for s, _, ev in results if s == strategy]
        first = collections.Counter(ev["reasons"][0]["code"] for ev in evs if ev["reasons"])
        allcodes = collections.Counter(r["code"] for ev in evs for r in ev["reasons"])
        summary[strategy] = dict(candidates=len(evs), by_category=gates.category_counts(evs),
                                 first_reason=dict(first.most_common()),
                                 any_reason=dict(allcodes.most_common()))
    eligible = [dict(strategy=s, asset=c["asset"], long=c["long"], short=c["short"],
                     net=ev.get("net"), components={k: ev.get(k) for k in
                                                    ("funding", "basis", "fees", "spread",
                                                     "reserve")})
                for s, c, ev in results if ev.get("eligible")]
    research = [dict(strategy=s, asset=c["asset"], long=c["long"], short=c["short"],
                     net=ev.get("net"))
                for s, c, ev in results if not ev["reasons"] and not ev.get("eligible")]
    books = collections.Counter()
    for r in rows:
        b = Provider(col, closes).book(r["venue"], r["asset"])
        books[(r["exchange_id"], "ok" if b and not b.get("empty") else "missing")] += 1
    out = dict(at=at, closes_end=closes_end, rows=len(rows), summary=summary,
               eligible=eligible, research_only=research[:50],
               books={"%s:%s" % k: v for k, v in books.items()},
               replay_missing=dict(replay.missing),
               label="Evaluation on OBSERVED public data; PAPER; not a profit claim"
               + ("; COUNTERFACTUAL: US session assumed open on off-hours prices"
                  if assume_open else ""))
    (d / ("evaluation_session_open.json" if assume_open else "evaluation.json")).write_text(json.dumps(out, indent=1, default=str),
                                       encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("rows", "summary", "books", "replay_missing")},
                     indent=1, default=str))
    print("eligible:", len(eligible), eligible[:10])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--fetch-more", action="store_true")
    ap.add_argument("--assume-session-open", action="store_true")
    ap.add_argument("--pilot-db", default=PILOT_DB)
    a = ap.parse_args()
    d = Path(a.dir).resolve()
    if str(d).lower().startswith("e:"):
        raise SystemExit("must not write on E:")
    d.mkdir(parents=True, exist_ok=True)
    if a.fetch or a.fetch_more:
        fetch(d, more=a.fetch_more)
    evaluate(d, a.pilot_db, a.assume_session_open)


if __name__ == "__main__":
    main()
