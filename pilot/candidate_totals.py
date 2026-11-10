"""Whole-list candidate totals from the pilot (/api/candidates, paged; not the top 40).
Each candidate in exactly one class; first-decisive codes of REJECTED are exclusive.
usage: python -m pilot.candidate_totals OUT.json"""
import collections
import json
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:18765/api/candidates?"


def page(strategy, offset):
    with urllib.request.urlopen(BASE + urllib.parse.urlencode(
            dict(strategy=strategy, offset=offset, limit=200)), timeout=900) as r:
        return json.load(r)


out = dict(at=time.time())
for s in ("funding", "convergence"):
    rows, off = [], 0
    while True:
        p = page(s, off)
        rows += p["rows"]
        off += len(p["rows"])
        if not p["rows"] or off >= p["total"]:
            break
    cls = collections.Counter(r["cls"] for r in rows)
    first = collections.Counter(r["first_code"] for r in rows if r["cls"] == "REJECTED")
    second = collections.Counter(c for r in rows if r["cls"] == "REJECTED" for c in r["secondary_codes"])
    research = collections.Counter(z.split(":")[0] + ": " + z.split(":", 1)[1].split("·")[0].strip()[:45]
                                   for r in rows if r["cls"] == "RESEARCH_CANDIDATE" for z in r["research"][:1])
    out[s] = dict(total=p["total"], counted=len(rows), classes=dict(cls), rejected_first_code=dict(first),
                  rejected_secondary_codes=dict(second),
                  research_first_reason_top=dict(research.most_common(12)),
                  paper_executable=[(r["asset"], r["long"], r["short"]) for r in rows if r["cls"] == "PAPER_EXECUTABLE"],
                  opened=[(r["asset"], r["long"], r["short"]) for r in rows if r["cls"] == "OPENED"],
                  identity=sum(1 for r in rows if any("Kimlik" in z for z in r["research"])))
    assert sum(cls.values()) == len(rows)  # input = pass + rejected + pending + research
json.dump(out, open(sys.argv[1], "w", encoding="utf8"), indent=1, ensure_ascii=False)
print(json.dumps(out, indent=1, ensure_ascii=False))
