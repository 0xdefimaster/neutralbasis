"""Legacy vs answer time basis on a SIMULATED live cadence (MOCK venue answers, fake
clock; nothing leaves the machine). The recorded data cannot show the difference (the
replays use one clock), so this measures it on the real Collector code paths:

  books    the engine reads a book every second through Collector.book (5 s cache)
           and the gate evaluates it `delay` seconds later; stale verdicts under both
           bases, per venue limit
  funding  rows rebuilt every 60 s (refresh_every); Lighter RH's rate comes from its
           own fundings answer cached 600 s; the legacy desk's entry rule wants the
           rate <= 120 s old: how often is it older under each basis

  python tools/v2_time_basis_sim.py
"""
import json
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v2 import marketdata  # noqa: E402
from v2.collector import TTL, Collector  # noqa: E402

T0 = 1_794_484_800.0
SECONDS = 3600


def books(delays=(0.0, 2.0, 6.0), limits=None):
    limits = limits or dict(txflow=10.0, arcus=15.0, lighter_rh=30.0)
    out = {}
    for d in delays:
        now = [T0]
        col = Collector(transport=lambda *a, **k: {"MOCK": 1}, clock=lambda: now[0],
                        mono=lambda: now[0])
        col.by_key = {("V", "X"): dict(exchange_id="arcus", market_id="X", multiplier=1.0)}
        res = {v: dict(checks=0, legacy_stale=0, answer_stale=0, flips=0, max_gap_s=0.0)
               for v in limits}
        with mock.patch("v2.venues.arcus.parse_book",
                        return_value=dict(bids=[(99.9, 1.0)], asks=[(100.1, 1.0)])):
            for s in range(SECONDS):
                now[0] = T0 + s
                b = col.book("V", "X")                  # read (cache hit or fetch)
                at = now[0] + d                          # the gate decides later
                for v, lim in limits.items():
                    leg = at - b["read_at"] > lim
                    ans = at - b["answer_received"] > lim
                    r = res[v]
                    r["checks"] += 1
                    r["legacy_stale"] += leg
                    r["answer_stale"] += ans
                    r["flips"] += ans and not leg
                    r["max_gap_s"] = max(r["max_gap_s"], b["read_at"] - b["answer_received"])
        out["gate_delay_%gs" % d] = res
    return out


def funding(entry_limit=120.0, refresh_every=60.0):
    now = [T0]
    col = Collector(transport=lambda *a, **k: {"MOCK": 1}, clock=lambda: now[0],
                    mono=lambda: now[0])
    last_refresh = None
    over = dict(legacy=0, answer=0)
    ages = dict(legacy=[], answer=[])
    for s in range(SECONDS):
        now[0] = T0 + s
        if last_refresh is None or now[0] - last_refresh >= refresh_every:
            col.get("rh:markets", TTL["meta"], lambda: {"MOCK": 1})
            col.get("rh:fund:1", TTL["settled"], lambda: {"MOCK": 1})
            last_refresh = now[0]
        a_leg = now[0] - last_refresh                    # legacy: row build time
        a_ans = now[0] - col.fetched_at("rh:fund:1")[0]  # answer: the rate's own fetch
        ages["legacy"].append(a_leg)
        ages["answer"].append(a_ans)
        over["legacy"] += a_leg > entry_limit
        over["answer"] += a_ans > entry_limit
    return dict(seconds=SECONDS, entry_limit_s=entry_limit,
                share_over_limit=dict(legacy=over["legacy"] / SECONDS,
                                      answer=over["answer"] / SECONDS),
                max_age_s=dict(legacy=max(ages["legacy"]), answer=max(ages["answer"])),
                note="under the answer basis an entry seeing an old rate re-reads it "
                     "(current_funding drops that market's cached answer): one extra "
                     "request per such entry attempt")


def build():
    return dict(evidence="SIMULATION (MOCK answers, fake clock, real Collector code)",
                books=books(), lighter_funding=funding())


if __name__ == "__main__":
    print(json.dumps(build(), indent=1))
