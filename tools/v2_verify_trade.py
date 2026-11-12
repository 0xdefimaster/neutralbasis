"""Line-by-line re-verification of one closed ESTIMATED PAPER trade (Phase 9 item 3).

PAPER / research only, offline. It reads the trade's ledger events and, INDEPENDENTLY,
the raw saved public answers (aligned bursts, stream), and recomputes:
  fills      IOC walked again through the same observed books (qty, price, slippage)
  fees       fee rate x qty x price per fill
  funding    every payment time: rate (settled history point or the published rate
             observed before it), units, valuation price, amount, evidence class
  price PnL  per leg from the recomputed fills
  gap        what the data gaps leave unknown, as BANDS from rates/prices actually
             observed at the gap's two ends (nothing interpolated, no path invented):
             the funding paid during the gap, the valuation price of settled payments,
             the exit delayed by the gap; and the observed mark-to-market path
Every recomputed number is compared with the ledger (|diff| <= 1e-9 USD -> MATCH).

Usage: python -m tools.v2_verify_trade --ledger L.jsonl --pos ID --aligned D... \
           --stream S.jsonl --out FILE.json
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v2 import economics, paper_exec, paper_obs, storage  # noqa: E402
from v2 import paper_ledger as L  # noqa: E402

TOL = 1e-9


def tr(t):
    return datetime.datetime.fromtimestamp(t + 3 * 3600, datetime.timezone.utc).strftime(
        "%H:%M:%S") if t else None


def match(a, b):
    return "MATCH" if a is not None and b is not None and abs(a - b) <= TOL else \
        "DIFF %.3g" % ((a or 0) - (b or 0))


def verify(ledger, pos, aligned, stream):
    recs, rep = storage.read_chain(ledger)
    ev = [r for r in recs if r.get("pos") == pos]
    opened = next(r for r in ev if r["kind"] == "open_attempt")
    obs = [paper_obs.from_aligned(f) for f in aligned]
    if stream:
        obs += paper_obs.from_stream(stream)[0]
    obs.sort(key=lambda o: o.at)
    by_at = {o.at: o for o in obs}
    legs = opened["legs"]
    asset = opened["asset"]
    out = dict(position=pos, ledger_chain=rep["status"], asset=asset,
               strategy=opened["strategy"], size=opened["size"],
               long=legs["long"]["venue"], short=legs["short"]["venue"], fills=[],
               funding=[], checks={})
    # ---------------------------------------------------------------- fills
    q_open = None
    recomputed = {}
    for f in [r for r in ev if r["kind"] == "fill"]:
        o = by_at[f["at"]]
        leg = legs[f["side"]]
        book = o.book(leg["venue"], asset)
        buy = (f["side"] == "long") == (f["action"] in ("open", "retry"))
        if f["action"] == "open" and q_open is None:
            qty = opened["size"] / economics.mid(book)
            q_open = qty
        else:
            qty = f["qty"]
        x = paper_exec.clob_ioc(book, buy, qty, paper_exec.DEFAULT_SLIPPAGE_BP,
                                leg["fee_rate"])
        recomputed[(f["side"], f["action"])] = x
        out["fills"].append(dict(
            at=tr(f["at"]), side=f["side"], venue=leg["venue"], action=f["action"],
            buy=buy, book_source_time=[tr(t) for t in f["obs_time"]],
            ledger=dict(qty=f["qty"], price=f["price"], fee=f["fee"]),
            recomputed=dict(qty=x["filled"], price=x["price"], fee=x["fee"],
                            mid=x.get("mid"), slippage_bp=x.get("slippage_bp"),
                            levels=x.get("levels")),
            check=dict(qty=match(x["filled"], f["qty"]), price=match(x["price"], f["price"]),
                       fee=match(x["fee"], f["fee"])),
            fee_rate=leg["fee_rate"], basis="SIMULATED IOC on the observed book"))
    # ---------------------------------------------------------------- price PnL
    pnl = {}
    for side in ("long", "short"):
        o_, c_ = recomputed[(side, "open")], recomputed[(side, "close")]
        sign = 1 if side == "long" else -1
        pnl[side] = sign * o_["filled"] * (c_["price"] - o_["price"])
    out["price_pnl"] = dict(per_leg=pnl, total=sum(pnl.values()))
    fees = sum(x["fee"] for x in recomputed.values())
    out["fees"] = fees
    # ---------------------------------------------------------------- funding
    marks = [r for r in ev if r["kind"] == "mark"]
    fund_total = {}
    for f in [r for r in ev if r["kind"] == "funding"]:
        evidence = L.ALIASES.get(f["evidence"], f["evidence"])
        side, T = f["side"], f["ts"]
        prev_mark = max((m for m in marks if m["at"] < T), key=lambda m: m["at"])
        venue = legs[side]["venue"]
        if evidence == "SETTLED_PUBLIC_RATE":
            o_after = next(o for o in obs if o.at >= T)
            pts = [h for h in o_after.settled(venue, asset, o_after.at)
                   if abs(h["ts"] - T) <= 60]
            rate = pts[0]["rate"] if pts else None
            src = "settled history point %s (read %s)" % (
                tr(pts[0]["ts"]) if pts else None, tr(o_after.at))
        else:
            o_prev = max((o for o in obs if o.at < T), key=lambda o: o.at)
            rate = o_prev.rows[(venue, asset)]["rate"]
            src = "published rate observed %s (%.0f s before)" % (tr(o_prev.at), T - o_prev.at)
        price = prev_mark["marks"][side]
        sign = -1 if side == "long" else 1
        amount = None if rate is None else sign * f["units"] * price * rate
        fund_total[evidence] = fund_total.get(evidence, 0.0) + (amount or 0.0)
        out["funding"].append(dict(
            payment=tr(T), ts=T, side=side, venue=venue, evidence=evidence, rate_source=src,
            rate=rate, units=f["units"], valuation_price=price,
            valuation_source="observed mark at %s" % tr(prev_mark["at"]),
            ledger_amount=f["amount"], recomputed_amount=amount,
            check=match(amount, f["amount"])))
    out["funding_by_evidence"] = fund_total
    total = out["price_pnl"]["total"] - fees + sum(fund_total.values())
    ledger_total = L.PaperLedger.pnl(rebuild(recs, pos))["realized_total"]
    out["net"] = dict(recomputed=total, ledger=ledger_total, check=match(total, ledger_total))
    close_at = max(r["at"] for r in ev if r["kind"] == "fill")
    out["gap"] = gap_analysis(out, obs, legs, asset, marks, opened, recomputed, close_at)
    out["checks"] = dict(
        fills=all(v == "MATCH" for f in out["fills"] for v in f["check"].values()),
        funding=all(f["check"] == "MATCH" for f in out["funding"]),
        net=out["net"]["check"] == "MATCH")
    return out


def rebuild(recs, pos):
    class _L(L.PaperLedger):
        def __init__(self):
            self.positions, self.signals, self.last_heartbeat = {}, [], None

    led = _L()
    for r in recs:
        led._apply(r)
    return led.positions[pos]


def gap_analysis(out, obs, legs, asset, marks, opened, recomputed, close_at):
    """Bands from what was observed at both ends of each gap; nothing in between."""
    lv, sv = legs["long"]["venue"], legs["short"]["venue"]
    seen = [o for o in obs if opened["at"] <= o.at <= close_at]
    gaps = [dict(start=tr(a.at), end=tr(b.at), hours=(b.at - a.at) / 3600)
            for a, b in zip(seen, seen[1:]) if b.at - a.at > 3600]
    res = dict(observations_during_position=len(seen), gaps_over_1h=gaps)
    # 1. TxFlow funding during the gap: carried rate vs the rate seen at the gap's end
    carried = [f for f in out["funding"] if f["evidence"] == "ASSUMED_CARRIED_FORWARD"]
    band = []
    for f in carried:
        after = [o for o in obs if o.at > f["ts"]]
        end_rate = after[0].rows[(f["venue"], asset)]["rate"] if after else None
        sign = -1 if f["side"] == "long" else 1
        alt = None if end_rate is None else sign * f["units"] * f["valuation_price"] * end_rate
        band.append(dict(payment=f["payment"], side=f["side"], booked=f["recomputed_amount"],
                         rate_booked=f["rate"], rate_at_gap_end=end_rate,
                         amount_at_gap_end_rate=alt))
    lo = sum(min(x["booked"], x["amount_at_gap_end_rate"]) for x in band
             if x["amount_at_gap_end_rate"] is not None)
    hi = sum(max(x["booked"], x["amount_at_gap_end_rate"]) for x in band
             if x["amount_at_gap_end_rate"] is not None)
    booked = sum(x["booked"] for x in band)
    res["carried_funding"] = dict(
        payments=band, booked_total=booked, band_from_observed_endpoints=[lo, hi],
        net_band=[out["net"]["recomputed"] - booked + lo,
                  out["net"]["recomputed"] - booked + hi],
        note="the true rates in the gap were never observed; the band uses only the "
             "rates seen at the gap's two ends (no interpolation)")
    # 2. Valuation price of settled payments (mark carried from the last observation)
    sp = [f for f in out["funding"] if f["evidence"] == "SETTLED_PUBLIC_RATE"]
    if sp:
        prices = [m["marks"]["short" if sp[0]["side"] == "short" else "long"] for m in marks]
        lo_p, hi_p = min(prices), max(prices)
        amt = sum(f["recomputed_amount"] for f in sp)
        res["settled_valuation"] = dict(
            payments=len(sp), booked=amt,
            band_over_observed_marks=[amt * lo_p / sp[0]["valuation_price"],
                                      amt * hi_p / sp[0]["valuation_price"]],
            note="tradeXYZ values payments at its oracle price, not in the history: "
                 "the amount is an estimate even though the rate is settled")
    # 3. Exit timing and the observed mark-to-market path
    path = []
    q = recomputed[("long", "open")]["filled"]
    lo_e, so_e = recomputed[("long", "open")]["price"], recomputed[("short", "open")]["price"]
    for o in seen:
        lb, sb = o.book(lv, asset), o.book(sv, asset)
        if not (lb and sb and lb.get("bids") and sb.get("bids")):
            continue
        lc = paper_exec.clob_ioc(lb, False, q, paper_exec.DEFAULT_SLIPPAGE_BP)
        sc = paper_exec.clob_ioc(sb, True, q, paper_exec.DEFAULT_SLIPPAGE_BP)
        carry = o.rows[(sv, asset)]["rate"] - o.rows[(lv, asset)]["rate"]
        path.append(dict(at=tr(o.at), carry_8h_pct=carry * 8 * 100,
                         exit_now_price_pnl=None if lc["price"] is None or sc["price"] is None
                         else q * (lc["price"] - lo_e) + q * (so_e - sc["price"])))
    res["observed_path"] = path
    res["exit_delay"] = dict(
        reversal_first_seen=next((p["at"] for p in path if p["carry_8h_pct"] <= 0), None),
        rule="FUNDING_REVERSED needs carry <= 0 for 30 min",
        earliest_possible_with_continuous_data="first observation >= 30 min after the "
        "reversal was seen; the reversal itself happened at an UNKNOWN time inside the gap",
        actual_close=path[-1]["at"] if path else None)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--pos", required=True)
    ap.add_argument("--aligned", nargs="*", default=[])
    ap.add_argument("--stream")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    storage.check_dir(out.parent)
    r = verify(a.ledger, a.pos, a.aligned, a.stream)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(r, indent=1, default=str), encoding="utf-8")
    print(json.dumps(dict(checks=r["checks"], net=r["net"], price_pnl=r["price_pnl"],
                          fees=r["fees"], funding_by_evidence=r["funding_by_evidence"],
                          gap={k: v for k, v in r["gap"].items() if k != "observed_path"}),
                     indent=1, default=str))


if __name__ == "__main__":
    main()
