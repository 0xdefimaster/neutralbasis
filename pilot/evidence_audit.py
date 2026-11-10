"""Read-only audit of the PAPER pilot's funding evidence, per venue and market.

Opens the PILOT database (C:, never the live E: one) with mode=ro and reads the
capability registry state: pending published-rate observations ("predictions"),
completed comparisons with settled payments ("published" samples), and the interval
checks. Prints what each venue still needs before its funding-sign gate can open.

usage: python -m pilot.evidence_audit [OUT.json]
"""
import collections
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

DATA = Path(os.environ.get("NB_PILOT_DATA", r"C:\Users\W10\nb-pilot\data"))
DB = DATA / "paper.sqlite3"
PUBLISHED_MIN, PUBLISHED_VENUE_MIN = 3, 3


def load():
    if str(DB).lower().startswith("e:"):
        raise SystemExit("refusing: not the pilot database")
    con = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True, timeout=30)
    try:
        row = con.execute("select v from kv where k='capability_state'").fetchone()
    finally:
        con.close()
    return json.loads(row[0]) if row else {}


def audit(state, at):
    venues = collections.defaultdict(lambda: dict(
        predicted_markets=0, pending=0, stale_pending=0, compared_markets=0,
        samples_hist=collections.Counter(), agree=0, disagree=0, ready_markets=0,
        interval_checked=0, interval_match=0, interval_undecided=0, examples=[]))
    for key, obs in (state.get("predictions") or {}).items():
        v = key.split("|")[0]
        x = venues[v]
        x["predicted_markets"] += 1
        for bucket, (seen, rate, nxt) in obs.items():
            x["pending"] += 1
            if nxt and seen >= nxt:
                x["stale_pending"] += 1
                if len(x["examples"]) < 3:
                    x["examples"].append(dict(market=key, seen=seen, next=nxt, rate=rate))
    for key, samples in (state.get("published") or {}).items():
        v = key.split("|")[0]
        x = venues[v]
        x["compared_markets"] += 1
        n = len(samples)
        x["samples_hist"][min(n, PUBLISHED_MIN)] += 1
        x["agree"] += sum(1 for s in samples if s[1])
        x["disagree"] += sum(1 for s in samples if not s[1])
        x["ready_markets"] += n >= PUBLISHED_MIN
    for key, iv in (state.get("intervals") or {}).items():
        v = key.split("|")[0]
        x = venues[v]
        x["interval_checked"] += 1
        x["interval_match"] += iv.get("match") is True
        x["interval_undecided"] += iv.get("match") is None
    out = {}
    for v, x in sorted(venues.items()):
        x["samples_hist"] = {"%d%s" % (k, "+" if k == PUBLISHED_MIN else ""): n
                             for k, n in sorted(x["samples_hist"].items())}
        x["gate_needs"] = (
            "open-able: %d markets have >= %d comparisons (venue needs %d decided)"
            % (x["ready_markets"], PUBLISHED_MIN, PUBLISHED_VENUE_MIN))
        out[v] = x
    return dict(at=at, db=str(DB), venues=out)


if __name__ == "__main__":
    res = audit(load(), time.time())
    text = json.dumps(res, indent=1, default=str)
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(text, encoding="utf8")
    print(text)
