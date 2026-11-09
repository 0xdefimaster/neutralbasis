"""Replay fidelity check for the CURRENT RFQ scheduler (PAPER ONLY; offline).

Reads JSONL written by rfq_trace_recorder.py and, for every recorded cycle, recomputes
from the cycle's own inputs:

  1. the stream priority order (engine key: (research or waiting or blocked, -score),
     stable over the funding list then the convergence list, first row per asset, only
     assets eligible for streaming) and compares it with the recorded `ranked`;
  2. the chosen stream set with extended_rfq.choose_streams(ranked, held, since, at,
     cap, hold) and compares it with the recorded `chosen` (Jaccard);
  3. the deep-check quota walk: replaying the recorded attempts with the engine's rule
     (stop when counted >= top or picked >= 4 * top; only `assessed` rows count, and
     only when slot_consumed) must reproduce every slot flag and the stop position.

A cycle missing any input is reported as INCOMPLETE and never counted as a match.
No scheduler is chosen and no performance is claimed here.
"""

import argparse, collections, glob, json, os, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from extended_rfq import choose_streams  # noqa: E402

TARGET_CYCLES = 100
TARGET_JACCARD = 0.99


def load(paths):
    cycles, metas = [], []
    for p in paths:
        with open(p, encoding="utf8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                if r.get("type") == "cycle":
                    cycles.append(r)
                elif r.get("type") == "meta":
                    metas.append(r)
    return cycles, metas


def priority_order(cycle):
    """The engine's stream priority, recomputed from recorded candidate rows."""
    alloc = cycle["allocation"]
    at, eligible = alloc["at"], set(alloc["eligible"])
    rows = sorted(cycle["rfq_candidates"],
                  key=lambda r: (0 if r[0] == "funding" else 1, r[1]))
    keyed = []
    for r in rows:
        (_, _, asset, _, _, score, _, _, research, until, _, blocked, _) = r
        waiting = bool(research) or (until is not None and until > at) or bool(blocked)
        keyed.append(((waiting, -(score or 0)), asset))
    keyed.sort(key=lambda kv: kv[0])  # stable, like sorted(..., key=stream_priority)
    ranked = []
    for _, a in keyed:
        if a in eligible and a not in ranked:
            ranked.append(a)
    return ranked


def jaccard(a, b):
    a, b = set(a), set(b)
    return 1.0 if not (a | b) else len(a & b) / len(a | b)


def walk_quota(cycle):
    """(match, detail) for the deep-check quota rule over the recorded attempts."""
    top = cycle.get("deep_top")
    if top is None:
        return None, "deep_top yok"
    by = collections.defaultdict(list)
    for r in cycle["deep_checks"]:
        by[r[0]].append(r)
    for strategy, rows in by.items():
        counted = picked = 0
        for r in rows:
            action, consumed = r[5], r[6]
            should_stop = counted >= top or picked >= top * 4
            if action == "stop_quota":
                if not should_stop:
                    return False, "%s: kota dolmadan durdu (order %s)" % (strategy, r[1])
                break
            if should_stop:
                return False, "%s: kota dolduktan sonra deneme (order %s)" % (strategy, r[1])
            if action == "assessed":
                picked += 1
                if consumed:
                    counted += 1
            elif consumed:
                return False, "%s: atlama hak tüketmiş (order %s)" % (strategy, r[1])
    return True, ""


def verify(cycles):
    out = dict(cycles=len(cycles), complete=0, incomplete=0, order_match=0,
               chosen_exact=0, jaccards=[], quota_match=0, quota_checked=0,
               mismatches=[], incomplete_reasons=collections.Counter())
    for c in cycles:
        alloc = c.get("allocation")
        missing = [k for k in ("eligible", "hold", "ranked", "since", "held", "chosen")
                   if not alloc or alloc.get(k) is None]
        if not c.get("rfq_candidates") and alloc and alloc.get("ranked"):
            missing.append("rfq_candidates")
        if missing:
            out["incomplete"] += 1
            out["incomplete_reasons"].update(missing)
            continue
        out["complete"] += 1
        ranked = priority_order(c)
        recorded = [a for a, _ in alloc["ranked"]]
        if ranked == recorded:
            out["order_match"] += 1
        else:
            out["mismatches"].append(dict(cycle_id=c["cycle_id"], kind="order"))
        chosen = choose_streams(recorded, set(alloc["held"]),
                                {a: t for a, t in alloc["since"].items()},
                                alloc["at"], alloc["cap"], alloc["hold"])
        j = jaccard(chosen, alloc["chosen"])
        out["jaccards"].append(j)
        if j == 1.0:
            out["chosen_exact"] += 1
        else:
            out["mismatches"].append(dict(cycle_id=c["cycle_id"], kind="chosen",
                                          jaccard=j))
        q, why = walk_quota(c)
        if q is not None:
            out["quota_checked"] += 1
            if q:
                out["quota_match"] += 1
            else:
                out["mismatches"].append(dict(cycle_id=c["cycle_id"], kind="quota",
                                              why=why))
    js = out.pop("jaccards")
    out["jaccard_mean"] = statistics.mean(js) if js else None
    out["jaccard_min"] = min(js) if js else None
    out["incomplete_reasons"] = dict(out["incomplete_reasons"])
    out["mismatches"] = out["mismatches"][:50]
    enough = out["complete"] >= TARGET_CYCLES
    out["verdict"] = (
        "INSUFFICIENT_DATA" if not enough else
        "PASS" if (out["jaccard_mean"] is not None and out["jaccard_min"] >= TARGET_JACCARD
                   and out["order_match"] == out["complete"]
                   and out["quota_match"] == out["quota_checked"]) else "FAIL"
    )
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("paths", nargs="+", help="JSONL files or folders")
    a = p.parse_args(argv)
    files = []
    for x in a.paths:
        files += sorted(glob.glob(os.path.join(x, "rfq-trace-*.jsonl"))) if os.path.isdir(x) else [x]
    cycles, metas = load(files)
    res = verify(cycles)
    res["recorder_meta"] = dict(collections.Counter(m.get("kind") for m in metas))
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
