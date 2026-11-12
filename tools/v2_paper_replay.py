"""Deterministic ESTIMATED PAPER replay: NATGAS / INIT from entry signal to close.

PAPER / research only, offline: it reads saved public answers (aligned bursts from
tools.v2_aligned, bounded streams from tools.v2_stream) and the pilot's hourly closes
(read-only, C:), runs v2.paper_engine observation by observation in time order, and
writes hash-chained ledgers plus reports. Nothing is traded; no network is used.

Runs (each its own ledger folder):
  estimated   NATGAS pairs inside the PAPER universe (TxFlow, tradeXYZ, Variational)
  scenario    INIT long Variational / short Hyperliquid native (observation-only leg):
              SCENARIO_ONLY, never part of the estimated PAPER portfolio
  stress_*    the same observations with injected faults: last-look rejections,
              a firm-quote gap, a crash after the first leg followed by a restart
Output folder: inside the dedicated data folder (v2.storage; E:\\nb-v2-phase8 by
default, owner decision 2026-11-12).

Usage: python -m tools.v2_paper_replay --out E:\\nb-v2-phase8\\replay-X
           --aligned DIR... --stream FILE...
"""
import argparse
import collections
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import wide_paper  # noqa: E402
from v2 import paper_engine, paper_obs, storage  # noqa: E402
from v2 import paper_ledger as L  # noqa: E402

PILOT_DB = r"C:\Users\W10\nb-pilot-loose\data\paper.sqlite3"
NATGAS_PAIRS = (("NATGAS", "TxFlow", "tradeXYZ"), ("NATGAS", "tradeXYZ", "TxFlow"),
                ("NATGAS", "Variational", "tradeXYZ"), ("NATGAS", "tradeXYZ", "Variational"),
                ("NATGAS", "TxFlow", "Variational"), ("NATGAS", "Variational", "TxFlow"))
INIT_PAIRS = (("INIT", "Variational", "Hyperliquid"), ("INIT", "Hyperliquid", "Variational"))


def closes_db():
    from tools.v2_phase4_eval import pilot_closes

    closes, _ = pilot_closes(PILOT_DB)
    return closes


def load(aligned, streams, closes):
    obs, sources = [], []
    for f in aligned:
        o = paper_obs.from_aligned(f, closes)
        obs.append(o)
        sources.append(dict(kind="aligned", path=str(f), sha256=o.sha, at=o.at))
    for s in streams:
        os_, report = paper_obs.from_stream(s, closes)
        obs.extend(os_)
        sources.append(dict(kind="stream", path=str(s), chain=report, observations=len(os_),
                            first=os_[0].at if os_ else None,
                            last=os_[-1].at if os_ else None))
    obs = [o for o in obs if o.at is not None]
    obs.sort(key=lambda o: o.at)
    return obs, sources


def run(folder, obs, pairs, scenario, **kw):
    cfg = dict(wide_paper.WIDE_DEFAULTS)
    led = L.PaperLedger(folder)
    eng = paper_engine.PaperEngine(led, cfg, pairs, scenario=scenario, **kw)
    restart_at = kw.get("crash_at")
    for o in obs:
        try:
            eng.step(o)
        except paper_engine.Crash:
            old_evals = eng.evaluations
            # the process "dies"; a new one starts on the same ledger folder
            led = L.PaperLedger(folder)
            eng = paper_engine.PaperEngine(led, cfg, pairs, scenario=scenario,
                                           **dict(kw, crash_at=None))
            eng.evaluations = old_evals
    return led, eng, restart_at


def report(led, eng, obs, asset):
    """The eight acceptance numbers per strategy for one asset."""
    last = {}
    for o in obs:
        for (v, a), b in o.books.items():
            if b and b.get("bids") and b.get("asks"):
                last[(v, a)] = (b["bids"][0][0] + b["asks"][0][0]) / 2
    out = {}
    for s in paper_engine.STRATEGIES:
        ev = [e for e in eng.evaluations if e["asset"] == asset and e["strategy"] == s]
        sig = [x for x in led.signals if x["asset"] == asset and x["strategy"] == s]
        pos = [p for p in led.positions.values() if p["asset"] == asset and
               p["strategy"] == s]
        filled = [p for p in pos if any(l["entry_qty"] > L.EPS for l in p["legs"].values())]
        closed = [p for p in filled if p["state"] == L.CLOSED]
        open_ = [p for p in filled if p["state"] != L.CLOSED]
        pn = {p["id"]: L.PaperLedger.pnl(p, {sd: last.get((lg["venue"], asset))
                                             for sd, lg in p["legs"].items()})
              for p in filled}
        fills = [f for p in pos for lg in p["legs"].values() for f in lg["fills"]]
        not_filled = [n for p in pos for n in p["reliability"] if "not filled" in n]
        rel = {p["id"]: (p["reliability"] or ["-"])[-1] for p in closed}
        out[s] = dict(
            candidates_evaluated=len(ev),
            candidate_pairs=len({(e["long"], e["short"]) for e in ev}),
            economically_interesting=sum(1 for e in ev if e["stage"] != L.DISCOVERED),
            entry_signals=len(sig),
            signals_blocked_by_time=sum(1 for e in ev if e["blocked_by_time"]),
            positions_created=len(filled),
            attempts_not_filled=len(pos) - len(filled),
            positions_closed=len(closed),
            closed_reliable=sum(1 for p in closed if rel[p["id"]] == "RELIABLE"),
            open_or_recovery=[dict(id=p["id"], state=p["state"],
                                   unhedged_units=led.exposure(p)) for p in open_],
            pnl=dict(
                observed_part=sum(pn[p["id"]]["observed_part"] for p in filled),
                estimated_part=sum(pn[p["id"]]["estimated_part"] for p in filled),
                realized_total_closed=sum(pn[p["id"]]["realized_total"] for p in closed),
                unrealized_open=sum(pn[p["id"]]["unrealized"] or 0.0 for p in open_),
                unvalued_open=sum(1 for p in open_ if pn[p["id"]]["unrealized"] is None)),
            components=dict(
                price_pnl=sum(pn[p["id"]]["price_pnl"] for p in filled),
                fees=sum(pn[p["id"]]["fees"] for p in filled),
                funding_by_evidence={k: sum(pn[p["id"]]["funding"][k] for p in filled)
                                     for k in L.FUNDING_KINDS}),
            fills=dict(total=len(fills), verified=sum(1 for f in fills if f["verified"]),
                       estimated_unverified=sum(1 for f in fills if not f["verified"]),
                       attempts_not_filled=len(not_filled)),
            first_rejections=dict(collections.Counter(
                (x["first"] or "-") for e in ev for x in e["sizes"]
                if x["status"] != "ESTIMATED_ENTRY_SIGNAL").most_common(8)))
    return out


def stories(led, obs):
    """Per signal: when, the position, the data, what happened next, why it closed,
    the net, observed vs estimated."""
    out = []
    for sig in led.signals:
        p = led.positions.get(L.pos_id(led.seed, sig))
        item = dict(signal_at=sig["at"], observation=sig["obs"], asset=sig["asset"],
                    strategy=sig["strategy"], long=sig["long"], short=sig["short"],
                    size=sig["size"], expected=sig["expected"], skew_s=sig["skew_s"],
                    freshness_limit_s=sig["freshness_limit_s"])
        if p is None:
            out.append(dict(item, position=None))
            continue
        pn = L.PaperLedger.pnl(p)
        carries = [(m["at"], m.get("carry_hourly")) for m in p["marks"]]
        item.update(position=p["id"], created=any(l["entry_qty"] > L.EPS
                                                  for l in p["legs"].values()),
                    state=p["state"], states=p["states"],
                    entry={s: dict(venue=l["venue"], model=l["model"],
                                   qty=l["entry_qty"], price=L.PaperLedger.entry_price(l),
                                   fills=l["fills"]) for s, l in p["legs"].items()},
                    next_observation=p["marks"][1] if len(p["marks"]) > 1 else None,
                    funding_changed=len({round(c or 0, 12) for _, c in carries}) > 1,
                    carry_path=carries[:5] + (carries[-3:] if len(carries) > 8 else []),
                    close_reason=p["close_reason"], secondary=p["secondary"],
                    closed_at=p["closed_at"], pnl=pn, funding=p["funding"],
                    reliability=p["reliability"][-1] if p["reliability"] else None)
        out.append(item)
    return out


def last_mids(obs, pairs):
    """{asset: last observed mid of the pairs' first leg with a book} for open risk."""
    out = {}
    for o in obs:
        for asset, lv, sv in pairs:
            for v in (lv, sv):
                b = o.books.get((v, asset))
                if b and b.get("bids") and b.get("asks"):
                    out[asset] = (b["bids"][0][0] + b["asks"][0][0]) / 2
                    break
    return out


def chain_head(folder):
    _, rep = storage.read_chain(Path(folder) / "ledger.jsonl")
    return rep["last_hash"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--aligned", nargs="*", default=[])
    ap.add_argument("--stream", nargs="*", default=[])
    a = ap.parse_args()
    out = storage.check_dir(a.out)
    out.mkdir(parents=True, exist_ok=True)
    closes = closes_db()
    obs, sources = load(a.aligned, a.stream, closes)
    runs = dict(
        estimated=dict(pairs=NATGAS_PAIRS, scenario=False, kw={}),
        scenario_init=dict(pairs=INIT_PAIRS, scenario=True, kw={}),
        stress_last_look=dict(pairs=NATGAS_PAIRS, scenario=False,
                              # every RFQ open is refused by the OLP (last look)
                              kw=dict(last_look=lambda pos, side, action, n:
                                      action == "open")),
        stress_firm_gap=dict(pairs=NATGAS_PAIRS, scenario=False, kw=dict(firm_gap_bp=10.0)),
        stress_crash_restart=dict(pairs=NATGAS_PAIRS, scenario=False,
                                  kw=dict(crash_at="after_first_leg")),
    )
    result = dict(label="ESTIMATED PAPER replay (simulated fills on observed public data;"
                  " no exchange order, no verified fill)", sources=sources,
                  integrity=paper_obs.integrity(obs, expected_every=20.0),
                  observations=len(obs), first=obs[0].at if obs else None,
                  last=obs[-1].at if obs else None, runs={})
    for name, r in runs.items():
        folder = out / name
        if (folder / "ledger.jsonl").exists():
            raise SystemExit("%s exists: a replay never appends to an old ledger" % folder)
        led, eng, _ = run(folder, obs, r["pairs"], r["scenario"], **r["kw"])
        assets = sorted({p[0] for p in r["pairs"]})
        result["runs"][name] = dict(
            folder=str(folder), chain_head=chain_head(folder),
            ledger_integrity=storage.read_chain(folder / "ledger.jsonl")[1]["status"],
            report={a_: report(led, eng, obs, a_) for a_ in assets},
            risk=led.risk(last_mids(obs, r["pairs"])))
        (folder / "stories.json").write_text(json.dumps(stories(led, obs), indent=1,
                                                        default=str), encoding="utf-8")
        (folder / "evaluations.json").write_text(json.dumps(eng.evaluations, default=str),
                                                 encoding="utf-8")
    # determinism: the estimated run once more in a scratch sub-folder, same head
    again = out / "determinism_check"
    run(again, obs, NATGAS_PAIRS, False)
    result["determinism"] = dict(first=result["runs"]["estimated"]["chain_head"],
                                 second=chain_head(again),
                                 identical=chain_head(again) ==
                                 result["runs"]["estimated"]["chain_head"])
    (out / "replay_report.json").write_text(json.dumps(result, indent=1, default=str),
                                            encoding="utf-8")
    print(json.dumps(dict(observations=len(obs), determinism=result["determinism"],
                          runs={k: v["report"] for k, v in result["runs"].items()}),
                     indent=1, default=str))


if __name__ == "__main__":
    main()
