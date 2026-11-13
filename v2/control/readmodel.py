"""Control Center read model (Phase 14 Slice E). PAPER ONLY, OFFLINE.

`build()` turns saved artifacts into a few small JSON files under
<bench>/v2-paper-data/control/readmodel/. The GET-only server (v2.control.server)
only READS those files; a panel refresh never reaches a venue and never runs a check.

Sources (all saved, all local):
  Doctor (v2.control.doctor.run, offline)           exchanges, markets, system
  latest phase1*-scan-* opportunity report           opportunities, readiness
  v2-phase8-rescue-*/replay-*/*/stories.json         PAPER positions (ESTIMATED)
  heartbeats / health history (v2.control.telemetry) recorder + history
  phase14-funding-persistence/report.json            funding persistence

Every payload carries generated_at, data_mode and a banner. Without a running recorder
the data mode is HISTORICAL and the banner says so. UNKNOWN stays null with a reason;
it is never turned into HEALTHY, VERIFIED or 0.

Redaction: keys that look like secrets are dropped. The local bench and home paths are
replaced by <bench> / <home>. Environment variables are never read into a payload.
"""
import gzip
import json
import os
import re
import time
from pathlib import Path

from v2 import paths, storage
from v2 import funding_semantics as fs
from v2.control import capability, doctor, model, telemetry

SCHEMA = 1
FILES = ("overview", "exchanges", "markets", "system", "opportunities",
         "opportunities_summary", "strategies_readiness", "paper_positions",
         "health_history", "funding_persistence")
SECRET_KEY = re.compile(r"(secret|token|passw|private|api[_-]?key|signature|signing|auth|"
                        r"cookie|mnemonic|seed|credential|nonce|session[_-]?id)", re.I)
BANNER_HISTORICAL = "HISTORICAL DATA - RECORDER NOT RUNNING. Nothing here is live."
PUBLISHED_ONLY = ("RATE_PUBLISHED", "RATE_SAMPLED_AT_CUTOFF")


def readmodel_dir(bench=None):
    return Path(bench or paths.bench()) / "v2-paper-data" / "control" / "readmodel"


def _refuse_failing_disk(p):
    s = str(Path(p).resolve()).lower()
    if s.startswith(("d:", "e:")):
        raise storage.StorageError("read model never writes on D:/E: (%s)" % p)


# ----------------------------------------------------------------------- redaction
def redact(obj, bench=None, home=None):
    bench = str(bench or paths.bench())
    home = home or str(Path.home())
    subs = [(bench, "<bench>"), (bench.replace("\\", "/"), "<bench>"),
            (home, "<home>"), (home.replace("\\", "/"), "<home>")]

    def s(x):
        for a, b in subs:
            if a and a in x:
                x = x.replace(a, b)
        return x

    def walk(x):
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items() if not SECRET_KEY.search(str(k))}
        if isinstance(x, (list, tuple)):
            return [walk(v) for v in x]
        if isinstance(x, str):
            return s(x)
        if isinstance(x, float) and x != x:
            return None                       # NaN is not JSON; unknown stays null
        return x
    return walk(obj)


# ------------------------------------------------------------------------- pieces
def latest(globbed):
    xs = sorted(globbed, key=lambda p: p.stat().st_mtime)
    return xs[-1] if xs else None


def load_scan(bench):
    rep = latest(Path(bench).glob("v2-paper-data/phase1*-scan-*/opportunity_report.json"))
    if rep is None:
        return None, []
    r = json.loads(rep.read_text(encoding="utf-8"))
    rows_f = rep.parent / "opportunities.jsonl.gz"
    rows = []
    if rows_f.exists():
        rows = [json.loads(x) for x in
                gzip.decompress(rows_f.read_bytes()).decode("utf-8").splitlines() if x]
    r["_path"] = str(rep)
    return r, rows


def flags(row):
    """Display flags that follow from the directive. They are not gate changes."""
    f = []
    ex, st = row.get("expected_net"), row.get("stress_net")
    if ex is not None and ex > 0 and (st is None or st < 0):
        f.append("NOT_SAFE_STRESS_NEGATIVE" if st is not None else "STRESS_UNKNOWN")
    ev = (row.get("funding") or {}).get("evidence") or [row.get("evidence_level")]
    if row.get("strategy") in ("funding", "hybrid") and any(e in PUBLISHED_ONLY for e in ev):
        f.append("PUBLISHED_RATE_VALID_ONE_PERIOD")
    if row.get("reference") in (None, "UNKNOWN"):
        f.append("HEDGE_REFERENCE_UNVERIFIED")
    if "variational" in (row.get("long_xid"), row.get("short_xid")):
        f.append("RFQ_INDICATIVE_NO_FIRM_QUOTE")
    if row.get("freshness") in (None, "UNKNOWN"):
        f.append("FRESHNESS_UNKNOWN")
    fm = row.get("funding_models") or {}
    for x in fm.get("flags") or []:
        if x == "BASELINE_ONLY_POSITIVE":
            f.append("BASELINE_ONLY_POSITIVE")
        elif x.startswith("PAYMENT_RELATION_UNKNOWN") and                 "PUBLISHED_PAYMENT_RELATION_UNKNOWN" not in f:
            f.append("PUBLISHED_PAYMENT_RELATION_UNKNOWN")
    return f


def opportunity(row, idx):
    fu = row.get("funding") or {}
    return dict(
        id="op-%05d" % idx, at=row.get("at"), asset=row.get("asset"),
        strategy=row.get("strategy"), long=row.get("long"), short=row.get("short"),
        long_xid=row.get("long_xid"), short_xid=row.get("short_xid"),
        size_usd=row.get("size"), gap_bp=row.get("gap_bp"),
        carry_8h=fu.get("carry_8h"), funding_evidence=fu.get("evidence"),
        expected_net=row.get("expected_net"), stress_net=row.get("stress_net"),
        breakeven_h=row.get("breakeven_h"), label=row.get("label"),
        freshness=row.get("freshness"), data_age_s=row.get("data_age_s"),
        evidence_level=row.get("evidence_level"), execution_model=row.get("execution_model"),
        reference=row.get("reference") or "UNKNOWN",
        strict_eligible=bool(row.get("strict_eligible")),
        strict_first_rejection=row.get("strict_first"),
        estimated_status=row.get("estimated_status"),
        estimated_first_rejection=row.get("estimated_first"),
        rejections=row.get("rejections") or [], risk_adjusted_score=row.get(
            "risk_adjusted_score"),
        flags=flags(row), data_source="HISTORICAL",
        # Phase 15 P0-B: baseline / persistence / adverse, ESTIMATED; None = UNKNOWN
        funding_models=row.get("funding_models"),
        # never "SAFE": no verified execution exists; a positive expected net with a
        # negative or unknown stress net is explicitly NOT_SAFE
        safety="NOT_SAFE" if (row.get("expected_net") or 0) > 0 and (
            row.get("stress_net") is None or row.get("stress_net") < 0) else
        "RESEARCH_ONLY")


def readiness(rows):
    out = {}
    for r in rows:
        s = out.setdefault(r["strategy"], dict(rows=0, raw_positive_edge=0,
                                               expected_net_positive=0, stress_positive=0,
                                               strict_eligible=0, estimated_candidates=0,
                                               strict_first={}, estimated_first={}))
        s["rows"] += 1
        s["raw_positive_edge"] += bool(r.get("edge_positive"))
        s["expected_net_positive"] += bool((r.get("expected_net") or 0) > 0)
        s["stress_positive"] += bool((r.get("stress_net") or 0) > 0)
        s["strict_eligible"] += bool(r.get("strict_eligible"))
        s["estimated_candidates"] += r.get("label") == "ESTIMATED_PAPER_CANDIDATE"
        for k, src in (("strict_first", "strict_first"), ("estimated_first", "estimated_first")):
            v = r.get(src)
            if v:
                s[k][v] = s[k].get(v, 0) + 1
    for s in out.values():
        for k in ("strict_first", "estimated_first"):
            s[k] = dict(sorted(s[k].items(), key=lambda kv: -kv[1]))
        s["status"] = ("PAPER_ELIGIBLE" if s["strict_eligible"] else
                       "ESTIMATED_RESEARCH_ONLY" if s["estimated_candidates"] else "BLOCKED")
    return out


def paper_positions(bench):
    out = []
    for f in sorted(Path(bench).glob("v2-phase8-rescue-*/replay-*/*/stories.json")):
        scenario = f.parent.name
        for s in json.loads(f.read_text(encoding="utf-8")):
            fills = [dict(leg=side, **{k: x.get(k) for k in (
                "at", "action", "qty", "price", "fee", "label", "model", "quote_age_s",
                "slippage_bp", "partial", "verified")})
                for side in ("long", "short")
                for x in ((s.get("entry") or {}).get(side) or {}).get("fills") or []]
            fund = s.get("funding") or []
            ev = sorted({x.get("evidence") for x in fund if x.get("evidence")})
            pnl = s.get("pnl") or {}
            problems = []
            if "PUBLISHED_CARRIED_FORWARD" in ev:
                problems.append("funding carried forward from an old published rate")
            if any(x["model"] == "RFQ_INDICATIVE" for x in fills):
                problems.append("RFQ leg: indicative quote, no firm quote")
            if pnl.get("unrealized") is None and s.get("state") != "CLOSED":
                problems.append("unrealized PnL UNKNOWN (no aligned marks)")
            if "PUBLISHED_ESTIMATE" in ev:
                problems.append("funding from published rates (payments UNVERIFIED)")
            out.append(dict(
                id=s.get("position"), scenario=scenario,
                scenario_only=scenario != "estimated", asset=s.get("asset"),
                strategy=s.get("strategy"), long=s.get("long"), short=s.get("short"),
                size_usd=s.get("size"), state=s.get("state"), opened_at=s.get("signal_at"),
                closed_at=s.get("closed_at"), close_reason=s.get("close_reason"),
                expected=dict(net=(s.get("expected") or {}).get("net"),
                              stress=(s.get("expected") or {}).get("net_min"),
                              breakeven_h=(s.get("expected") or {}).get("breakeven_h"),
                              assumptions=(s.get("expected") or {}).get("assumptions")),
                fills=fills, funding_evidence=ev, pnl=pnl,
                reliability=s.get("reliability"),
                # no fill = nothing to value: null (not applicable), never "reliable"
                valuation_reliable=None if not fills else not problems,
                valuation_problems=problems if fills else
                ["no fill: rejected or never opened (nothing to value)"],
                label="ESTIMATED_PAPER: simulated on observed public data; not an exchange "
                      "position; fills are estimates, not real fills",
                data_source="ESTIMATED"))
    return out


def funding_persistence(bench):
    f = Path(bench) / "v2-paper-data" / "phase14-funding-persistence" / "report.json"
    # Phase 15 P0: what each venue's funding field means (registry, no market data)
    sem = fs.matrix()
    if not f.exists():
        return dict(status="NOT_RUN", reason="run tools/v2_funding_persistence.py",
                    semantics=sem)
    r = json.loads(f.read_text(encoding="utf-8"))
    return dict(status="OK", label=r.get("label"), snapshot_at=r.get("snapshot_at"),
                semantics=sem,
                by_venue=r.get("by_venue"),
                long_history={k: dict(confidence=v["confidence"],
                                      metrics={x: v["metrics"].get(x) for x in (
                                          "points", "span_h", "mean", "ar1_phi",
                                          "half_life_h", "sign_persistence",
                                          "reversals_per_day")},
                                      validation={x: v["validation"].get(x) for x in (
                                          "status", "mae", "best_model",
                                          "coverage_long_receives", "sign_kept_24h")})
                              for k, v in (r.get("long_history") or {}).items()},
                natgas_case={k: (r.get("natgas_case") or {}).get(k) for k in (
                    "reference", "models", "next_observations", "xyz_settled_around",
                    "txflow_payment_evidence", "notional_usd", "hold_h")},
                txflow_periods=(r.get("txflow_natgas") or {}).get("periods"))


def market_rows(opps, doctor_markets):
    """One row per reference asset seen by the scanner, plus the Doctor's per-market
    records (WS integrity). Research view; no new evaluation."""
    by = {}
    for o in opps:
        m = by.setdefault(o["asset"], dict(asset=o["asset"], venues=set(), strategies={},
                                           labels={}, rows=0, best_expected_net=None,
                                           best_stress_net=None, top_rejection={},
                                           freshness={}, at=o["at"]))
        m["rows"] += 1
        m["venues"].update(x for x in (o["long"], o["short"]) if x)
        for k, v in (("strategies", o["strategy"]), ("labels", o["label"]),
                     ("freshness", o["freshness"] or "UNKNOWN"),
                     ("top_rejection", o["estimated_first_rejection"])):
            if v:
                m[k][v] = m[k].get(v, 0) + 1
        if o["expected_net"] is not None and (m["best_expected_net"] is None or
                                              o["expected_net"] > m["best_expected_net"]):
            m["best_expected_net"], m["best_stress_net"] = o["expected_net"], o["stress_net"]
    out = []
    for m in by.values():
        m["venues"] = sorted(m["venues"])
        m["top_rejection"] = max(m["top_rejection"], key=m["top_rejection"].get)             if m["top_rejection"] else None
        m["doctor"] = [r for r in doctor_markets if str(r.get("market_id")) == m["asset"]]
        m["data_source"] = "HISTORICAL"
        out.append(m)
    out.sort(key=lambda m: (-(m["best_expected_net"] if m["best_expected_net"] is not None
                              else -1e9), m["asset"]))
    return dict(items=out, doctor_records=doctor_markets)


# -------------------------------------------------------------------------- build
def build(now=None, bench=None, out=None):
    now = now or time.time()
    bench = Path(bench or paths.bench())
    out = Path(out or readmodel_dir(bench))
    _refuse_failing_disk(out)
    storage.check_dir(out)
    doc = doctor.run(now)
    recorder = telemetry.read_heartbeat(doctor.control_dir(), "recorder", now=now)
    live = recorder["status"] == "RUNNING"
    data_mode = "LIVE_OBSERVED" if live else "HISTORICAL"
    banner = None if live else BANNER_HISTORICAL
    head = dict(schema=SCHEMA, generated_at=now, data_mode=data_mode, banner=banner,
                trading_mode="PAPER", live_trading="DISABLED")

    by_venue = {}
    for r in doc["records"]:
        by_venue.setdefault(r["exchange_id"], []).append(r)
    exchanges = []
    for v, m in doc["matrix"].items():
        recs = by_venue.get(v, [])
        worst = model.worst(r["health_status"] for r in recs) if recs else "NOT_TESTED"
        problems = [dict(dimension=r["dimension"], channel=r["channel"],
                         status=r["health_status"], reason=r["health_reason"])
                    for r in recs if r["health_status"] in (
                        "DISCONNECTED", "RATE_LIMITED", "INTEGRITY_FAILURE", "STALE",
                        "DEGRADED", "UNVERIFIED")]
        exchanges.append(dict(
            exchange_id=v, paper_venue=m["paper_venue"],
            observation_venue=m["observation_venue"], venue_status=m["status"],
            health=worst, last_successful_verification=m["last_successful_verification"],
            last_verification_age_s=m["last_verification_age_s"],
            last_verification_current=m["last_verification_current"],
            capabilities=m["capabilities"], records=recs, problems=problems,
            data_source="HISTORICAL"))
    doctor_markets = [r for r in doc["records"] if r.get("market_id") is not None]
    system_recs = by_venue.get("system", [])

    scan, rows = load_scan(bench)
    opps = [opportunity(r, i) for i, r in enumerate(rows)]
    markets = market_rows(opps, doctor_markets)
    order = {"ESTIMATED_PAPER_CANDIDATE": 0, "WATCHLIST": 1, "BLOCKED_ECONOMICS": 2,
             "DATA_STALE": 3}
    top = sorted([o for o in opps if o["expected_net"] is not None or o["label"] ==
                  "ESTIMATED_PAPER_CANDIDATE"],
                 key=lambda o: (order.get(o["label"], 9), -(o["expected_net"] or -1e9)))[:10]
    summary = dict(
        report=scan and scan.get("_path"), snapshot_at=scan and scan.get("at"),
        snapshot_age_s=None if not scan else now - scan["at"],
        note=scan and scan.get("note"), counts=scan and scan.get("counts"),
        discovery=scan and scan.get("DISCOVERY"), economic=scan and scan.get("ECONOMIC"),
        execution_readiness=scan and scan.get("EXECUTION_READINESS"),
        strict_first_rejection=scan and scan.get("strict_first_rejection"),
        estimated_first_rejection=scan and scan.get("estimated_first_rejection"),
        venues_in_discovered=scan and scan.get("venues_in_discovered"),
        top=top, data_source="HISTORICAL")
    positions = paper_positions(bench)
    fp = funding_persistence(bench)
    hist = dict(recorder=telemetry.read_history(doctor.control_dir(), "recorder", 86400, now),
                doctor_runs=[])
    for f in sorted(doctor.control_dir().glob("doctor-2*.json"))[-5:]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            hist["doctor_runs"].append(dict(at=d.get("generated_at"),
                                            sections=d.get("sections")))
        except Exception:
            continue

    venue_problems = [dict(exchange_id=e["exchange_id"], health=e["health"],
                           paper_venue=e["paper_venue"],
                           problems=len(e["problems"]),
                           last_verification_age_s=e["last_verification_age_s"])
                      for e in exchanges]
    sysd = {r["channel"]: dict(status=r["health_status"], reason=r["health_reason"])
            for r in system_recs}
    overview = dict(
        system_running=dict(recorder=recorder["status"],
                            paper_engine=telemetry.read_heartbeat(
                                doctor.control_dir(), "paper_engine", now=now)["status"],
                            control_server="SERVING (this API)"),
        system=sysd, last_observation_at=max([e["last_successful_verification"] or 0
                                              for e in exchanges]) or None,
        venues=venue_problems,
        opportunities=dict(counts=summary["counts"], top=top[:5],
                           snapshot_at=summary["snapshot_at"],
                           strict_eligible=(summary["execution_readiness"] or {}).get(
                               "strict_paper_eligible")),
        paper_positions=dict(total=len(positions),
                             open=sum(1 for p in positions if p["state"] != "CLOSED"),
                             reliable=sum(1 for p in positions if p["valuation_reliable"]),
                             never_filled=sum(1 for p in positions
                                              if p["valuation_reliable"] is None)),
        funding_persistence=dict(status=fp["status"]),
        doctor_sections=doc["sections"],
        data_kinds=dict(live="not available: recorder not running" if not live else
                        "recorder heartbeat RUNNING",
                        historical="saved snapshots and streams (see each item's time)",
                        simulation="PAPER positions and fills are ESTIMATED"))
    payloads = dict(
        overview=overview, exchanges=dict(items=exchanges), markets=markets,
        system=dict(items=system_recs, clock=sysd.get("clock")),
        opportunities=dict(items=opps), opportunities_summary=summary,
        strategies_readiness=dict(items=readiness(rows), note="STRICT is unchanged; "
                                  "ESTIMATED candidates are research only"),
        paper_positions=dict(items=positions), health_history=hist,
        funding_persistence=fp)
    out.mkdir(parents=True, exist_ok=True)
    sizes = {}
    for name in FILES:
        body = redact(dict(head, **payloads[name]), bench=bench)
        data = json.dumps(body, default=str, separators=(",", ":"), allow_nan=False)
        tmp = out / (name + ".json.tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, out / (name + ".json"))
        sizes[name] = len(data)
    manifest = dict(head, files=sizes, network_attempts=None,
                    sources=redact(dict(scan=summary["report"]), bench=bench))
    (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


VENUE_IDS = capability.VENUES
