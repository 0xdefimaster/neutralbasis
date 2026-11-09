"""Operator commands for the isolated PAPER pilot (its own API on 127.0.0.1:18765 only).

  python -m pilot.pilot_api configure   one-time: workspace on every selectable perp DEX
                                         (funding + convergence), documented fees
  python -m pilot.pilot_api report      write the current evidence to DATA/reports/
                                         (candidates, first decisive reasons, positions)

PAPER ONLY. It talks to the pilot's own loopback API, never to the live engine (8765).
"""
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

PORT = int(os.environ.get("NB_PILOT_PORT", "18765"))
DATA = Path(os.environ.get("NB_PILOT_DATA", r"C:\Users\W10\nb-pilot\data"))
BASE = "http://127.0.0.1:%d" % PORT
if PORT == 8765:
    raise SystemExit("Refusing: 8765 is the live engine")
# Documented fees (official docs read 2026-11-10). Lighter RH Premium taker 0.035 %;
# the account type is not verified, so the zero Standard fee is not used.
PILOT_CONFIG = {"lighter_fee": 0.00035}


def get(path, timeout=600):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.load(r)


def token():
    with urllib.request.urlopen(BASE + "/", timeout=30) as r:
        html = r.read().decode("utf8", "replace")
    m = re.search(r"window\.paperToken='([0-9a-f]{32})'", html)
    if not m:
        raise SystemExit("pilot panel token not found")
    return m.group(1)


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-Paper-Token": token()})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.load(r)


def configure():
    ident = get("/api/identity")
    if ident.get("mode") != "PAPER" or ident.get("pid") == 5988:
        raise SystemExit("not the PAPER pilot: %s" % ident)
    post("/api/config", PILOT_CONFIG)
    state = get("/api/state")
    choices = state["wide_paper"].get("workspace_choices") or []
    venues = {c["venue"]: 1000.0 for c in choices if c.get("selectable")}
    out = post("/api/wide_workspace", dict(venues=venues,
                                           strategies=dict(funding=True, convergence=True)))
    print(json.dumps(dict(configured=sorted(venues), config=PILOT_CONFIG,
                          result=str(out)[:300]), indent=1))


def report():
    at = time.time()
    state = get("/api/state")
    wnt = get("/api/why-no-trade")
    caps = get("/api/capabilities")
    wp = state["wide_paper"]
    from zero_trade import code_of

    def first(reasons):
        return code_of(reasons[0])[0] if reasons else None

    assessed = [dict(strategy=x.get("strategy"), asset=x["asset"], long=x["long"], short=x["short"],
                     screen=x.get("screen_score"), net=x.get("net"), eligible=x.get("eligible"),
                     first_reason=(x.get("reasons") or [None])[0], first_code=first(x.get("reasons")),
                     other_codes=[code_of(r)[0] for r in (x.get("reasons") or [])[1:]])
                for x in wp.get("assessed") or []]
    cands = {}
    for strat, lst in (("funding", wp.get("candidates") or []), ("convergence", wp.get("conv_candidates") or [])):
        for c in lst:
            k = "%s|%s|%s|%s" % (strat, c["asset"], c["long"], c["short"])
            cands[k] = dict(score=c.get("score"), research=c.get("research"), rank=c.get("rank"))
    rep = dict(
        at=at, identity=get("/api/identity"), last_cycle=wp.get("last_cycle"),
        workspace=wp.get("workspace"), funnel=wp.get("funnel"), review=wp.get("review"),
        venues={v: dict(blocked=x.get("strategies"), discovery=x["caps"]["market_discovery"]["method"])
                for v, x in (caps.get("venues") or {}).items()},
        candidates_unique=len(cands),
        candidates_research=sum(1 for c in cands.values() if c["research"]),
        candidates=cands, assessed=assessed,
        positions=wp.get("positions"), closed=wp.get("closed"),
        why_no_trade=dict(verdict=wnt.get("verdict"), windows={k: w.get("reason_codes") for k, w in (wnt.get("windows") or {}).items()}),
    )
    out = DATA / "reports"
    out.mkdir(parents=True, exist_ok=True)
    path = out / time.strftime("pilot-report-%Y%m%d-%H%M%S.json", time.localtime(at))
    path.write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str), encoding="utf8")
    print(json.dumps(dict(file=str(path), last_cycle=rep["last_cycle"],
                          candidates_unique=rep["candidates_unique"],
                          research=rep["candidates_research"], assessed=len(assessed),
                          eligible=sum(1 for a in assessed if a["eligible"]),
                          positions=len(rep["positions"] or []), closed=len(rep["closed"] or []),
                          venues_blocked={v: x["blocked"] for v, x in rep["venues"].items()}),
                     ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    {"configure": configure, "report": report}[sys.argv[1]]()
