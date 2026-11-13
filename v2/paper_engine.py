"""ESTIMATED PAPER engine: entry signal -> simulated execution -> monitoring -> exit ->
simulated close, on a sequence of observed market moments (PAPER only, deterministic).

Nothing here sends an order, signs, or reads an account. It is driven by
tools.v2_paper_replay (offline) or tests; the runtime does not create it. The strict
gate (v2.gates) and the strict desk (v2.hybrid_desk) are not touched.

Per observation (`step`), in order:
  1. every open position progresses: retry a partial entry, recover a failed one,
     continue a pending close, or (hedged) book funding, mark, and check exits
  2. every followed pair x strategy without an open position (and outside the
     wide_cooldown_seconds after its last close or failed attempt) is evaluated at each
     size (v2.estimated, strict thresholds); the best risk-adjusted size gives an
     ENTRY_SIGNAL only when the pair's legs are time-aligned for the market's
     volatility; the signal is simulated (v2.paper_exec) into a position

Size choice (Phase 8 F): sizes 1,000 / 2,500 / 5,000 / 10,000 USD; among the sizes
whose estimate is an entry, the one with the highest WORST-scenario net
(uncertainty.net_min) wins, ties to the smaller size. A size with no fill model (no
depth, no exact RFQ tier) is never chosen; nothing is invented to fill it.

Exits (checked only on a time-aligned observation; MAX_HOLD always):
  funding      FUNDING_REVERSED (carry <= 0 for wide_weak_minutes), RISK_LIMIT
               (marked loss beyond min(wide_risk, wide_conv_stop_pct of size)),
               MAX_HOLD (wide_max_hold_hours)
  convergence  CONVERGED (gap back within wide_conv_exit_bps of the baseline), STOP
               (gap 6 x MAD beyond the entry deviation), RISK_LIMIT, MAX_HOLD
               (wide_conv_max_hold_hours)
  hybrid       two component exits - convergence done (CONVERGED seen) and funding
               done (FUNDING_REVERSED) - the position closes when BOTH are done, or
               at once on STOP / RISK_LIMIT / MAX_HOLD

Crash safety (Phase 9):
  heartbeat         one `heartbeat` event per step (time, open positions, unhedged USD)
  restart           the first step after a restart emits `restart` with the downtime
                    since the last heartbeat and, per position, the unhedged seconds;
                    positions are handled in order of unhedged USD (largest first)
  reconcile         each leg's quantity is recomputed from its fills; a mismatch moves
                    the position to RECOVERY_PENDING (never trusted silently)
  recovery timeout  a missing hedge is completed only while the position has been
                    unhedged for at most `recovery_timeout_s` (default 60 s); later the
                    risk is REDUCED first: the excess leg is unwound at the current book
                    instead of hedging hours later at a different price. Without data to
                    act, the position stays RECOVERY_PENDING and its exposure is reported.

Entry gate (Phase 9 item 8): the default (`STRICT_THRESHOLDS`) is unchanged: an entry
needs net >= wide_min_net, break-even <= 24 h, combined_moderate >= 0, rfq_moderate >= 0
and the reference stress >= 0. Other, harsher scenarios (exit spread x3, +5 bp per
fill, funding -50 %, half-filled second leg, firm gap 25 bp, last look 20 %, sign flip)
are REPORTED in net_min but do not gate, so a signal can carry a negative worst case.
`WORST_CASE_NONNEGATIVE` (opt-in, owner decision) also requires net_min >= 0; every
evaluation records `alt_gate_blocks` either way.
"""
import math

from v2 import economics, estimated, gates, paper_exec, rfq_paper
from v2 import paper_ledger as L

SIZES = (1000.0, 2500.0, 5000.0, 10000.0)
STRATEGIES = ("funding", "convergence", "hybrid")
HOUR = 3600.0


class Crash(RuntimeError):
    """Fault injection: the process dies at a chosen point (tests)."""


def cfg_at(cfg, size):
    return dict(cfg, wide_size=size)


class PaperEngine:
    def __init__(self, ledger, cfg, pairs, strategies=STRATEGIES, sizes=SIZES,
                 scenario=False, max_retries=2, slippage_bp=paper_exec.DEFAULT_SLIPPAGE_BP,
                 firm_gap_bp=0.0, last_look=None, crash_at=None, recovery_timeout_s=60.0,
                 entry_gate="STRICT_THRESHOLDS"):
        if entry_gate not in ("STRICT_THRESHOLDS", "WORST_CASE_NONNEGATIVE"):
            raise ValueError("unknown entry gate %r" % entry_gate)
        self.recovery_timeout_s, self.entry_gate = recovery_timeout_s, entry_gate
        self.resumed = False
        self.ledger, self.cfg, self.pairs = ledger, cfg, list(pairs)
        self.strategies, self.sizes, self.scenario = strategies, sizes, scenario
        self.max_retries, self.slippage_bp, self.firm_gap_bp = (max_retries, slippage_bp,
                                                                firm_gap_bp)
        self.last_look = last_look or (lambda pos, side, action, n: False)
        self.last_look_simulated = last_look is not None
        self.crash_at = crash_at
        self.evaluations = []

    # ================================================================== signals
    def evaluate(self, obs, asset, lv, sv, strategy):
        c = dict(asset=asset, long=lv, short=sv, strategy=strategy)
        skew, lim, basis = obs.pair_time(lv, sv, asset)
        aligned = skew is not None and skew <= lim
        table = []
        for size in self.sizes:
            cfg = cfg_at(self.cfg, size)
            ev = estimated.EstimatedEvaluator(gates.Gate(obs, cfg), cfg, obs.rfq_record)
            r = ev.scenario(c, obs.at, strategy) if self.scenario else ev.evaluate(
                c, obs.at, strategy)
            status = r.get("scenario_result", r["status"]) if self.scenario else r["status"]
            table.append(dict(size=size, status=status, first=r.get("first"),
                              net=r.get("net"),
                              net_min=(r.get("uncertainty") or {}).get("net_min"),
                              result=r))
        entries = [x for x in table if x["status"] == estimated.ENTERED
                   and x["net_min"] is not None]
        strict_best = max(entries, key=lambda x: (x["net_min"], -x["size"])) \
            if entries else None
        alt_blocks = bool(strict_best) and strict_best["net_min"] < 0
        if self.entry_gate == "WORST_CASE_NONNEGATIVE":
            entries = [x for x in entries if x["net_min"] >= 0]
        best = max(entries, key=lambda x: (x["net_min"], -x["size"])) if entries else None
        interesting = any(x["net"] is not None for x in table)
        stage = (L.ENTRY_SIGNAL if best and aligned else
                 L.INTERESTING if interesting else L.DISCOVERED)
        rec = dict(at=obs.at, obs=obs.origin, asset=asset, long=lv, short=sv,
                   strategy=strategy, stage=stage, skew_s=skew, freshness_limit_s=lim,
                   freshness_basis=basis, time_quality="ALIGNED" if aligned else
                   "TIME_MISALIGNED_FOR_VOLATILITY",
                   sizes=[{k: x[k] for k in ("size", "status", "first", "net", "net_min")}
                          for x in table],
                   chosen_size=best["size"] if best else None,
                   blocked_by_time=bool(best) and not aligned,
                   entry_gate=self.entry_gate, alt_gate_blocks=alt_blocks,
                   strict_best_net_min=strict_best["net_min"] if strict_best else None,
                   gating_stresses=self.gating(strict_best["result"]) if strict_best
                   else None)
        self.evaluations.append(rec)
        return rec, (best["result"] if best and aligned else None)

    @staticmethod
    def gating(r):
        """Which scenarios gate the entry and which only lower net_min."""
        st = {x["name"]: x["net"] for x in r.get("stress") or []}
        rq = {x["name"]: x["net"] for x in r.get("rfq_stress") or []}
        fs = {x["name"]: x["net"] for x in (r.get("funding_scenarios") or {}).get(
            "scenarios", [])}
        gate = dict(combined_moderate=st.get("combined_moderate"),
                    rfq_moderate=rq.get("rfq_moderate"),
                    reference_gap_x1=(r.get("reference_stress") or {}).get("net"))
        info = {k: v for d in (st, rq) for k, v in d.items()
                if k not in ("base", "combined_moderate", "rfq_moderate") and v is not None}
        info.update({"funding:" + k: v for k, v in fs.items() if v is not None})
        worst = min(info.items(), key=lambda kv: kv[1]) if info else None
        return dict(gating=gate, informational_worst=worst,
                    negative_informational=sorted(k for k, v in info.items() if v < 0))

    # ================================================================== step
    def step(self, obs):
        if not self.resumed:
            self.resume(obs)
        order = sorted(self.ledger.open_positions(),
                       key=lambda p: -abs(self.ledger.exposure(p)))
        for p in order:
            self.progress(p, obs)
        for asset, lv, sv in self.pairs:
            if (lv, asset) not in obs.rows or (sv, asset) not in obs.rows:
                continue
            for strategy in self.strategies:
                same = [p for p in self.ledger.positions.values()
                        if p["strategy"] == strategy and p["asset"] == asset and
                        p["legs"]["long"]["venue"] == lv and p["legs"]["short"]["venue"] == sv]
                if any(p["state"] not in L.FINAL for p in same):
                    continue
                # never hold the opposite direction on the same two venues (any
                # strategy): the two positions would cancel each other's exposure
                if any(p["asset"] == asset and p["legs"]["long"]["venue"] == sv and
                       p["legs"]["short"]["venue"] == lv
                       for p in self.ledger.open_positions()):
                    continue
                # cooldown after a close or a failed attempt (wide_cooldown_seconds)
                if any(p["closed_at"] is not None and obs.at - p["closed_at"] <
                       self.cfg.get("wide_cooldown_seconds", 0) for p in same):
                    continue
                rec, result = self.evaluate(obs, asset, lv, sv, strategy)
                if result is None:
                    continue
                sig = self.ledger.emit("signal", at=obs.at, obs=obs.origin,
                                       asset=asset, long=lv, short=sv, strategy=strategy,
                                       size=rec["chosen_size"], status=L.ENTRY_SIGNAL,
                                       scenario_only=self.scenario,
                                       sizes=rec["sizes"], skew_s=rec["skew_s"],
                                       freshness_limit_s=rec["freshness_limit_s"],
                                       expected=self.expected(result))
                self.open(sig, result, obs)
        self.heartbeat(obs)

    def resume(self, obs):
        """First step of this process: report the downtime and every restarted
        position's unhedged time, reconcile legs against fills."""
        self.resumed = True
        restarted = [p for p in self.ledger.open_positions() if p.get("restarted")]
        if not restarted:
            return
        last = self.ledger.last_heartbeat
        items = []
        for p in sorted(restarted, key=lambda p: -abs(self.ledger.exposure(p))):
            secs, since = L.PaperLedger.unhedged(p, obs.at)
            bad = L.PaperLedger.reconcile(p)
            items.append(dict(pos=p["id"], state=p["state"],
                              unhedged_units=self.ledger.exposure(p),
                              unhedged_seconds=secs, unhedged_since=since,
                              reconcile=bad or "OK"))
            if bad:
                self.state(p, obs, L.RECOVERY_PENDING, "RECONCILE_MISMATCH")
        self.ledger.emit("restart", at=obs.at, last_heartbeat=last,
                         downtime_s=None if last is None else obs.at - last,
                         positions=items)

    def heartbeat(self, obs):
        mids = {}
        risk = self.ledger.risk(at=obs.at)
        self.ledger.emit("heartbeat", at=obs.at, open=len(risk),
                         unhedged=[dict(id=r["id"], units=r["unhedged_units"],
                                        seconds=r["unhedged_seconds"])
                                   for r in risk if abs(r["unhedged_units"]) > L.EPS])
        return mids

    @staticmethod
    def expected(r):
        return dict(net=r.get("net"), net_min=(r.get("uncertainty") or {}).get("net_min"),
                    carry_hourly=r.get("carry_hourly"), breakeven_h=r.get("breakeven_h"),
                    components=r.get("components"),
                    assumptions=[a.get("label") for a in r.get("assumptions") or []],
                    funding_scenarios=[{k: x.get(k) for k in ("name", "funding", "net")}
                                       for x in (r.get("funding_scenarios") or {}).get(
                                           "scenarios", [])])

    # ================================================================== execution
    def leg_meta(self, obs, side, venue, asset):
        row = obs.rows[(venue, asset)]
        book = obs.book(venue, asset)
        model = "RFQ" if (book or {}).get("model") == "rfq_indicative" else "CLOB"
        return dict(venue=venue, side=side, exchange_id=row.get("exchange_id"),
                    market_id=row.get("market_id"), model=model, fee_rate=row.get("fee"),
                    interval=row.get("interval"))

    def sim_fill(self, p, side, obs, action, qty=None, size_usd=None):
        """One simulated fill; books a `fill` event when anything filled."""
        leg = p["legs"][side]
        buy = (side == "long") == (action in ("open", "retry"))
        v, a = leg["venue"], p["asset"]
        n = sum(1 for f in leg["fills"] if f["action"] == action)
        times = obs.times.get((v, a)) or []
        if leg["model"] == "RFQ":
            rec = obs.rfq_record(v, a)
            if self.last_look(p["id"], side, action, n):
                f = dict(reason="LAST_LOOK_REJECTED (scenario)", filled=0.0)
            else:
                f = paper_exec.rfq_fill(rec, buy, size_usd or p["size"], obs.at,
                                        fee_rate=leg["fee_rate"] or 0.0,
                                        firm_gap_bp=self.firm_gap_bp, qty=qty)
        else:
            book = obs.book(v, a)
            if qty is None:
                qty = (size_usd or p["size"]) / economics.mid(book) if book and \
                    book.get("bids") and book.get("asks") else 0.0
            f = paper_exec.clob_ioc(book, buy, qty, self.slippage_bp, leg["fee_rate"] or 0.0)
        if f.get("filled", 0.0) <= L.EPS:
            self.ledger.emit("reliability", pos=p["id"], note="%s %s %s not filled at %.0f: %s"
                             % (action, side, v, obs.at, f.get("reason")))
            return 0.0
        self.ledger.emit("fill", pos=p["id"], side=side, at=obs.at, action=action,
                         qty=f["filled"], price=f["price"], fee=f["fee"], label=f["label"],
                         model=f["model"], obs_time=times, quote_age_s=f.get("quote_age_s"),
                         tier=f.get("tier"), slippage_bp=f.get("slippage_bp"),
                         partial=f.get("partial"), verified=False,
                         last_look="SCENARIO" if self.last_look_simulated else
                         "NOT_SIMULATED_IN_BASE_RUN" if leg["model"] == "RFQ" else None)
        return f["filled"]

    def open(self, sig, result, obs):
        asset, lv, sv = sig["asset"], sig["long"], sig["short"]
        pid = L.pos_id(self.ledger.seed, sig)
        legs = {"long": self.leg_meta(obs, "long", lv, asset),
                "short": self.leg_meta(obs, "short", sv, asset)}
        exit_params = dict(baseline=result.get("baseline"), mad=result.get("mad"),
                           entry_dev=result.get("deviation"),
                           hold_h=result.get("expected_hold_h"),
                           carry_hourly=result.get("carry_hourly"))
        self.ledger.emit("open_attempt", pos=pid, at=obs.at, strategy=sig["strategy"],
                         asset=asset, size=sig["size"], signal=sig["obs"], legs=legs,
                         expected=sig["expected"], exit_params=exit_params,
                         scenario_only=self.scenario)
        p = self.ledger.positions[pid]
        p["time_quality_open"] = "ALIGNED"
        # The leg with execution uncertainty (RFQ, last look) goes first; the hedge
        # leg is sized to what the first leg actually filled.
        order = sorted(("long", "short"), key=lambda s: (legs[s]["model"] != "RFQ", s))
        first = self.sim_fill(p, order[0], obs, "open")
        if self.crash_at == "after_first_leg":
            raise Crash("injected crash after the first leg")
        if first <= L.EPS:
            self.state(p, obs, L.CLOSED, "ENTRY_NOT_FILLED")
            return p
        self.sim_fill(p, order[1], obs, "open", qty=first)
        self.settle_entry(p, obs)
        return p

    def settle_entry(self, p, obs):
        if abs(self.ledger.exposure(p)) <= L.EPS * max(1.0, p["legs"]["long"]["qty"]):
            self.state(p, obs, L.HEDGED)
            self.state(p, obs, L.MONITORING)
            self.mark(p, obs)
        else:
            self.state(p, obs, L.PARTIAL, "legs unequal: %.6g long vs %.6g short"
                       % (p["legs"]["long"]["qty"], p["legs"]["short"]["qty"]),
                       retries=p["retries"])

    def state(self, p, obs, state, reason=None, **kw):
        self.ledger.emit("state", pos=p["id"], at=obs.at, state=state, reason=reason, **kw)

    # ================================================================== progress
    def progress(self, p, obs):
        st = p["state"]
        if st == L.ATTEMPT:
            # restart (or crash) during the attempt: what filled is in the ledger
            self.state(p, obs, L.PARTIAL, "RESTART_DURING_ATTEMPT", retries=p["retries"])
            st = L.PARTIAL
        if st == L.PARTIAL:
            return self.retry(p, obs)
        if st == L.LEG_RECOVERY:
            return self.recover(p, obs)
        if st in (L.EXIT_SIGNAL, L.CLOSE_ATTEMPT):
            return self.close(p, obs)
        if st == L.RECOVERY_PENDING:
            return self.close(p, obs) if p["close_reason"] else self.recover(p, obs)
        if st in (L.HEDGED, L.MONITORING):
            if st == L.HEDGED:
                self.state(p, obs, L.MONITORING)
            return self.monitor(p, obs)

    def retry(self, p, obs):
        x = self.ledger.exposure(p)
        short_side = "short" if x > 0 else "long"
        missing = abs(x)
        leg = p["legs"][short_side]
        got = 0.0
        secs, since = L.PaperLedger.unhedged(p, obs.at)
        if (self.recovery_timeout_s is not None and since is not None and
                obs.at - since > self.recovery_timeout_s):
            # Too late to complete the hedge at the entry's terms: reduce risk first.
            self.state(p, obs, L.LEG_RECOVERY, "RECOVERY_TIMEOUT: unhedged %.0f s > %.0f s;"
                       " unwind first" % (obs.at - since, self.recovery_timeout_s),
                       retries=p["retries"])
            return self.recover(p, obs)
        if leg["model"] == "CLOB" or p["legs"][short_side]["qty"] <= L.EPS:
            got = self.sim_fill(p, short_side, obs, "retry", qty=missing)
        else:
            self.ledger.emit("reliability", pos=p["id"], note="RFQ remainder %.6g units "
                             "is not an exact tier: no retry" % missing)
        retries = p["retries"] + 1
        if got > L.EPS and abs(self.ledger.exposure(p)) <= L.EPS * max(1.0, missing):
            self.state(p, obs, L.HEDGED, "retry filled", retries=retries)
            self.state(p, obs, L.MONITORING)
            self.mark(p, obs)
        elif retries >= self.max_retries:
            self.state(p, obs, L.LEG_RECOVERY, "retries exhausted", retries=retries)
            self.recover(p, obs)
        else:
            self.state(p, obs, L.PARTIAL, "retry %d" % retries, retries=retries)

    def recover(self, p, obs):
        """Unwind the excess of the larger leg; the hedged remainder (if any) is kept
        and monitored; flat -> CLOSED (LEG_RECOVERY_FLAT)."""
        x = self.ledger.exposure(p)
        if abs(x) > L.EPS:
            side = "long" if x > 0 else "short"
            self.sim_fill(p, side, obs, "unwind", qty=abs(x), size_usd=None)
        x = self.ledger.exposure(p)
        lq, sq = p["legs"]["long"]["qty"], p["legs"]["short"]["qty"]
        if abs(x) > L.EPS * max(1.0, lq, sq):
            self.state(p, obs, L.RECOVERY_PENDING, "unhedged %.6g units remain" % x)
        elif lq <= L.EPS and sq <= L.EPS:
            self.book_pending_funding(p, obs)
            self.state(p, obs, L.CLOSED, "LEG_RECOVERY_FLAT")
        else:
            self.state(p, obs, L.HEDGED, "recovered to the smaller leg")
            self.state(p, obs, L.MONITORING)

    # ================================================================== monitoring
    def mids(self, p, obs):
        out = {}
        for side, leg in p["legs"].items():
            b = obs.book(leg["venue"], p["asset"])
            out[side] = economics.mid(b) if b and b.get("bids") and b.get("asks") else None
        return out

    def snapshot(self, p, obs):
        m = self.mids(p, obs)
        pn = L.PaperLedger.pnl(p, m)
        rates = {s: (obs.rows.get((leg["venue"], p["asset"])) or {}).get("rate")
                 for s, leg in p["legs"].items()}
        marks_ = {s: (obs.rows.get((leg["venue"], p["asset"])) or {}).get("mark")
                  for s, leg in p["legs"].items()}
        carry = None if None in rates.values() else rates["short"] - rates["long"]
        gap = (math.log(m["short"] / m["long"]) if m["long"] and m["short"] else None)
        x = self.ledger.exposure(p)
        mid_any = m["long"] or m["short"]
        return dict(pos=p["id"], at=obs.at, unrealized=pn["mark_to_market"], gap=gap,
                    carry_hourly=carry, rates=rates, marks=marks_, mids=m,
                    net_open_risk=None if mid_any is None else abs(x) * mid_any)

    def mark(self, p, obs, extra=None):
        e = self.snapshot(p, obs)
        e.update(extra or {})
        self.ledger.emit("mark", **e)
        return e

    def last_mark(self, p):
        return p["marks"][-1] if p["marks"] else {}

    def payment_times(self, p, side, obs, since, until):
        """Payment times of one leg in (since, until], from the venue's published next
        payment (TxFlow, Variational) or from its settled history times (hourly on the
        hour, Hyperliquid / tradeXYZ). [] and a note when unknown."""
        leg = p["legs"][side]
        row = obs.rows.get((leg["venue"], p["asset"])) or {}
        iv = row.get("interval") or leg.get("interval")
        nxt = row.get("next_funding")
        if not iv:
            return None
        if not nxt:
            hist = obs.settled(leg["venue"], p["asset"], obs.at) or []
            ts = [h["ts"] for h in hist[-24:]]
            if len(ts) >= 3 and all(int(t) % int(iv) == 0 for t in ts):
                nxt = math.floor(obs.at / iv) * iv + iv
            else:
                return None
        out, t = [], nxt - iv
        while t > since:
            if t <= until:
                out.append(t)
            t -= iv
        return sorted(out)

    def book_funding(self, p, obs):
        """Book every payment time passed since the last mark, once each."""
        prev = self.last_mark(p)
        since = max(p["opened_at"], (prev or {}).get("at") or p["opened_at"])
        for side, leg in p["legs"].items():
            if leg["qty"] <= L.EPS:
                continue
            times = self.payment_times(p, side, obs, since, obs.at)
            if times is None:
                self.ledger.emit("reliability", pos=p["id"], note="funding schedule "
                                 "UNKNOWN for %s %s" % (side, leg["venue"]))
                continue
            iv = (obs.rows.get((leg["venue"], p["asset"])) or {}).get("interval") or HOUR
            # history times carry milliseconds (e.g. 10:00:00.039): a point within 60 s
            # of a payment time is that payment
            hist = {}
            for h in obs.settled(leg["venue"], p["asset"], obs.at) or []:
                slot = round(h["ts"] / iv) * iv
                if abs(h["ts"] - slot) <= 60:
                    hist[slot] = h
            has_history = bool(hist)
            for T in times:
                self.book_payment(p, side, leg, T, hist.get(T), has_history, prev, obs)

    def book_payment(self, p, side, leg, T, settled, has_history, prev, obs):
        sign = -1 if side == "long" else 1
        iv = leg.get("interval") or HOUR
        key = "funding:%s:%d" % (side, int(T))
        if key in p["funding_keys"]:
            return
        price = ((prev or {}).get("marks") or {}).get(side) or \
            (obs.rows.get((leg["venue"], p["asset"])) or {}).get("mark")
        if settled is not None:
            # A settled point's rate is per PAYMENT PERIOD; `amount` below multiplies an
            # hourly rate by the period, so convert once (identical for hourly venues;
            # Phase 15 P0-C, latent gap G5).
            rate = settled["rate"] / ((settled.get("interval_s") or iv) / HOUR)
            evidence, basis = "SETTLED_PUBLIC_RATE", "public settled rate x observed mark " \
                "(venue price basis not in history: value estimated)"
        elif has_history:
            return                              # not published yet: wait (pending)
        else:
            r_prev = ((prev or {}).get("rates") or {}).get(side)
            near = prev and T - prev.get("at", -1e18) <= iv
            if r_prev is None:
                return
            rate = r_prev
            evidence = "PUBLISHED_RATE_ESTIMATE" if near else "ASSUMED_CARRIED_FORWARD"
            basis = "published rate observed %.0f s before the payment" % (
                T - prev["at"]) if prev else "published"
        units = leg["qty"]
        amount = sign * units * price * rate * (iv / HOUR)
        self.ledger.emit("funding", pos=p["id"], key=key, side=side, ts=T, rate=rate,
                         units=units, price=price, amount=amount, evidence=evidence,
                         basis=basis)

    def book_pending_funding(self, p, obs):
        """At close: payments of a settled-history leg that never appeared in the
        history read are booked from the last published rate, labelled."""
        prev = self.last_mark(p)
        for side, leg in p["legs"].items():
            times = self.payment_times(p, side, obs, p["opened_at"], obs.at) or []
            for T in times:
                key = "funding:%s:%d" % (side, int(T))
                if key in p["funding_keys"]:
                    continue
                r = ((prev or {}).get("rates") or {}).get(side)
                price = ((prev or {}).get("marks") or {}).get(side)
                if r is None or price is None:
                    continue
                units = max(leg["entry_qty"] - leg["exit_qty"], 0.0) or leg["entry_qty"]
                self.ledger.emit("funding", pos=p["id"], key=key, side=side, ts=T, rate=r,
                                 units=units, price=price,
                                 amount=(-1 if side == "long" else 1) * units * price * r *
                                 ((leg.get("interval") or HOUR) / HOUR),
                                 evidence="ASSUMED_CARRIED_FORWARD",
                                 basis="settled point missing at close")

    def monitor(self, p, obs):
        lv, sv = p["legs"]["long"]["venue"], p["legs"]["short"]["venue"]
        have = (lv, p["asset"]) in obs.rows and (sv, p["asset"]) in obs.rows
        if have:
            self.book_funding(p, obs)
        skew, lim, _ = obs.pair_time(lv, sv, p["asset"]) if have else (None, None, None)
        aligned = have and skew is not None and skew <= lim
        prev = self.last_mark(p)
        reasons, extra = [], dict(weak_since=prev.get("weak_since"),
                                  components_done=prev.get("components_done"))
        held_h = (obs.at - p["opened_at"]) / HOUR
        max_hold = (self.cfg["wide_conv_max_hold_hours"] if p["strategy"] == "convergence"
                    else self.cfg["wide_max_hold_hours"])
        if have:
            snap = self.snapshot(p, obs)
            if aligned:
                reasons, extra = self.exit_reasons(p, obs, snap, prev)
            self.mark(p, obs, dict(extra, aligned=aligned))
        if held_h >= max_hold:
            reasons.append("MAX_HOLD")
        if not aligned:
            self.ledger.emit("reliability", pos=p["id"], note="exit check skipped at %.0f:"
                             " data %s" % (obs.at, "missing" if not have else
                                           "time-misaligned (%.1f s > %.1f s)"
                                           % (skew if skew is not None else -1, lim)))
        if reasons:
            self.state(p, obs, L.EXIT_SIGNAL, reasons[0], secondary=reasons[1:],
                       time_quality="ALIGNED" if aligned else "MISALIGNED")
            self.close(p, obs)

    def exit_reasons(self, p, obs, snap, prev):
        """(reasons, persisted flags). Flags (weak_since, components_done) go into the
        mark event, so a restart resumes them."""
        cfg, ep = self.cfg, p.get("exit_params") or {}
        out = []
        carry = snap.get("carry_hourly")
        weak_since = prev.get("weak_since")
        if carry is not None and carry <= 0:
            weak_since = weak_since or obs.at
        else:
            weak_since = None
        reversed_ = weak_since is not None and \
            obs.at - weak_since >= cfg["wide_weak_minutes"] * 60
        pn = L.PaperLedger.pnl(p, snap["mids"])
        mtm = (pn["mark_to_market"] or 0.0) + pn["realized_total"]
        limit = min(cfg["wide_risk"], p["size"] * cfg["wide_conv_stop_pct"] / 100.0)
        if mtm < -limit:
            out.append("RISK_LIMIT")
        gap = snap.get("gap")
        conv_done = stop = False
        if ep.get("baseline") is not None and gap is not None:
            mad = ep.get("mad") or 0.0
            if mad and gap - ep["baseline"] >= 6 * mad + (ep.get("entry_dev") or 0.0):
                stop = True
            conv_done = gap <= ep["baseline"] + cfg["wide_conv_exit_bps"] / 1e4
        done = dict(prev.get("components_done") or {})
        if p["strategy"] == "funding":
            if reversed_:
                out.append("FUNDING_REVERSED")
        elif p["strategy"] == "convergence":
            if stop:
                out.append("STOP")
            if conv_done:
                out.append("CONVERGED")
        else:   # hybrid: two component exits, closed when both are done
            if conv_done:
                done.setdefault("convergence", obs.at)
            if reversed_:
                done.setdefault("funding", obs.at)
            if stop:
                out.append("STOP")
            if "convergence" in done and "funding" in done:
                out.append("BOTH_COMPONENTS_DONE")
        return out, dict(weak_since=weak_since, components_done=done or None,
                         mtm=mtm, risk_limit=limit)

    # ================================================================== close
    def close(self, p, obs):
        if p["state"] != L.CLOSE_ATTEMPT:
            self.state(p, obs, L.CLOSE_ATTEMPT)
        order = sorted(("long", "short"), key=lambda s: (p["legs"][s]["model"] != "RFQ", s))
        for side in order:
            leg = p["legs"][side]
            if leg["qty"] <= L.EPS:
                continue
            if leg["model"] == "RFQ":
                # an RFQ close must be an exact tier: the open notional
                self.sim_fill(p, side, obs, "close", qty=leg["qty"], size_usd=p["size"])
            else:
                self.sim_fill(p, side, obs, "close", qty=leg["qty"])
        lq, sq = p["legs"]["long"]["qty"], p["legs"]["short"]["qty"]
        if lq <= L.EPS and sq <= L.EPS:
            self.book_pending_funding(p, obs)
            self.state(p, obs, L.CLOSED, p["close_reason"])
            self.reliability(p)
        else:
            self.state(p, obs, L.RECOVERY_PENDING, "close incomplete: long %.6g short %.6g"
                       " units open" % (lq, sq))

    def reliability(self, p):
        """A closed position is RELIABLE only when open and close were time-aligned,
        no funding was carried forward, and no restart or recovery happened."""
        why = []
        if p.get("time_quality_close") not in (None, "ALIGNED"):
            why.append("CLOSE_TIME_MISALIGNED")
        if any(f["evidence"] == "ASSUMED_CARRIED_FORWARD" for f in p["funding"]):
            why.append("FUNDING_CARRIED_FORWARD")
        if p.get("restarted"):
            why.append("RESTARTED")
        if any(s[1] in (L.LEG_RECOVERY, L.RECOVERY_PENDING, L.PARTIAL) for s in p["states"]):
            why.append("RECOVERY")
        self.ledger.emit("reliability", pos=p["id"],
                         note="RELIABLE" if not why else "UNRELIABLE:" + ",".join(why))
