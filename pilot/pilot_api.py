"""Operator commands for the isolated PAPER pilot (its own API on 127.0.0.1:18765 only).

  python -m pilot.pilot_api configure   one-time: workspace on every selectable perp DEX
                                         (funding + convergence), documented fees
  python -m pilot.pilot_api mirror-fees copy the live engine's owner-entered taker fees
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
# Owner-entered taker fees copied from the LIVE engine's own setting (`wide_fees`, source
# "kullanıcı girişi" in its capability registry, read 2026-11-10). Mirrored so the pilot
# ranks the same venues as the live engine; no venue document backs them.
LIVE_USER_FEES = {"RISEx": 0.0003}


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


def mirror_fees():
    ident = get("/api/identity")
    if ident.get("mode") != "PAPER" or ident.get("pid") == 5988:
        raise SystemExit("not the PAPER pilot: %s" % ident)
    print(json.dumps(dict(mirrored=LIVE_USER_FEES, result=str(post("/api/wide_fees",
                                                                    dict(fees=LIVE_USER_FEES)))[:200])))


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
    # Every visible candidate in exactly one class. RESEARCH: shown with its limits; the
    # screen score is not a net and the net stays UNKNOWN. PAPER_EXECUTABLE: passed the
    # deep check (all gates). REJECTED: first decisive code. PENDING: not deep-checked.
    done = {(a["strategy"], a["asset"], a["long"], a["short"]): a for a in assessed}
    opened = {(p.get("strategy"), p["asset"], p["decision"]["long"], p["decision"]["short"])
              for p in (wp.get("positions") or []) if p.get("decision")}
    cands = {}
    for strat, lst in (("funding", wp.get("candidates") or []), ("convergence", wp.get("conv_candidates") or [])):
        for c in lst:
            key = (strat, c["asset"], c["long"], c["short"])
            a = done.get(key)
            if c.get("research"):
                cls = "RESEARCH_CANDIDATE"
            elif a is None:
                cls = "PENDING_DEEP_CHECK"
            elif a["eligible"]:
                cls = "PAPER_EXECUTABLE"
            else:
                cls = "REJECTED"
            cands["%s|%s|%s|%s" % key] = dict(
                cls=cls, rank=c.get("rank"), screen_not_net_bp=round((c.get("score") or 0) * 1e4, 1),
                net_usd=None if a is None else a["net"],
                limits=c.get("research") or [], first_code=None if a is None else a["first_code"],
                opened=key in opened)
    hidden = dict(funding=max(0, (wp.get("candidate_count") or 0) - len(wp.get("candidates") or [])),
                  convergence=max(0, (wp.get("conv_candidate_count") or 0) - len(wp.get("conv_candidates") or [])))
    rep = dict(
        at=at, identity=get("/api/identity"), last_cycle=wp.get("last_cycle"),
        workspace=wp.get("workspace"), funnel=wp.get("funnel"), review=wp.get("review"),
        venues={v: dict(blocked=x.get("strategies"), discovery=x["caps"]["market_discovery"]["method"])
                for v, x in (caps.get("venues") or {}).items()},
        candidates_unique=len(cands),
        classes={k: sum(1 for c in cands.values() if c["cls"] == k) for k in
                 ("RESEARCH_CANDIDATE", "PENDING_DEEP_CHECK", "REJECTED", "PAPER_EXECUTABLE")},
        beyond_api_top40=hidden,
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
                          classes=rep["classes"], beyond_api_top40=hidden, assessed=len(assessed),
                          eligible=sum(1 for a in assessed if a["eligible"]),
                          positions=len(rep["positions"] or []), closed=len(rep["closed"] or []),
                          venues_blocked={v: x["blocked"] for v, x in rep["venues"].items()}),
                     ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    {"configure": configure, "report": report, "mirror-fees": mirror_fees}[sys.argv[1]]()
