"""Funding expectation models, reported separately (Phase 15 P0-B). PAPER research.

A published funding value is valid for its CURRENT period at most
(v2/funding_semantics). Every later hour of a hold is a MODEL, and the three models
are always reported side by side; none of them is a forecast and all are ESTIMATED:

  baseline      the published value held for the whole horizon (the old
                PUBLISHED_RATE_CONSTANT assumption; kept for comparison only)
  persistence   the published value until the next payment, then
                  with settled history: AR(1) reversion mu + (r - mu) phi^k
                  without history, with published samples: their mean
                  with neither: UNKNOWN (None; the cash is then partial)
  adverse       side-aware: the leg moves AGAINST the holder
                  with settled history: r + q90 (long pays more) or r + q10 (short
                  receives less) of the 24 h-ahead residuals of the history
                  without history: q90 / q10 of the published samples after the
                  current period; with neither: UNKNOWN

No look-ahead: a fit uses settled points at or before `at` only. Rates are fractions
per HOUR, positive = longs pay. Cash is the hourly accrual of the carry (short receives
minus long pays) x notional; the discrete payment schedule is v2/funding_math.

Confidence never exceeds MEDIUM (no account payment evidence exists). A leg whose
published value has an UNKNOWN relation to the payment (TxFlow, Variational) is LOW at
best even for the current period.
"""
import math
import statistics

from v2 import funding_persistence as fp
from v2 import funding_semantics as fs

HOUR = 3600.0
MODELS = ("baseline", "persistence", "adverse")
MIN_POINTS = 72          # as funding_persistence.confidence
MEDIUM_SPAN_H = 30 * 24
SNAP_S = 300.0           # a settled point more than 5 min off its grid is dropped
EVIDENCE_CLASS = "ESTIMATED"


def _q(xs, p):
    xs = sorted(xs)
    return xs[int(p * (len(xs) - 1))]


def settled_series(history, at, interval_s=HOUR, exchange_id=None):
    """Settled points at or before `at` -> clean [(grid ts, rate per HOUR)].
    Accepts adapter points (ts, rate per period, kind) or contract settlements;
    projections are refused by v2/funding_semantics.settlement."""
    iv = interval_s or HOUR
    out = {}
    for p in history or []:
        if p.get("record") == "settlement":
            s = p
            ts = p.get("paid_at")
        else:
            s = fs.settlement(exchange_id, None, dict(p, interval_s=p.get("interval_s") or iv))
            ts = p.get("ts")
        if s is None or s.get("rate_per_hour") is None or ts is None or ts > at:
            continue
        slot = round(ts / iv) * iv
        if abs(ts - slot) <= SNAP_S:
            out[slot] = s["rate_per_hour"]
    return sorted(out.items())


def fit(series, interval_s=HOUR, horizon_h=24):
    """AR(1) and 24 h-ahead residual quantiles of a settled series (already cut at
    `at`). dict(status OK / INSUFFICIENT_HISTORY, mean, phi, step_h, resid_q10/q90,
    points, span_h)."""
    n = len(series)
    if n < MIN_POINTS:
        return dict(status="INSUFFICIENT_HISTORY", points=n)
    iv = interval_s or HOUR
    step_h = iv / HOUR
    rs = [r for _, r in series]
    mu, phi = fp.ar1(rs)
    by_t = dict(series)
    k = max(1, int(round(horizon_h / step_h)))
    resid = []
    for t, r in series:
        xs = [by_t.get(t + j * iv) for j in range(1, k + 1)]
        if all(x is not None for x in xs):
            resid.append(statistics.mean(xs) - r)
    if len(resid) < 10:
        return dict(status="INSUFFICIENT_HISTORY", points=n, reason="too few 24 h windows")
    return dict(status="OK", mean=mu, phi=phi, step_h=step_h, resid_q10=_q(resid, 0.10),
                resid_q90=_q(resid, 0.90), points=n,
                span_h=(series[-1][0] - series[0][0]) / HOUR, windows=len(resid))


def leg(exchange_id, rate_h, at, next_ts=None, interval_s=None, history=None,
        samples=None, side="long", fit_override=None):
    """One leg's inputs for `expect`. `samples`: published per-hour values seen at or
    before `at` (venues without settled history). `fit_override`: a precomputed fit
    (research reports); otherwise fitted here from `history`."""
    series = settled_series(history, at, interval_s, exchange_id) if history else []
    f = fit_override or (fit(series, interval_s) if series else
                         dict(status="NO_HISTORY", points=0))
    smp = [x for x in (samples or []) if isinstance(x, (int, float)) and x == x]
    if next_ts is not None and next_ts > at:
        first_h, first_basis = max(1, math.ceil((next_ts - at) / HOUR)), "NEXT_PAYMENT"
    else:
        first_h, first_basis = 1, "NEXT_PAYMENT_UNKNOWN: 1 h assumed"
    return dict(exchange_id=exchange_id, side=side, rate_h=rate_h, at=at,
                first_period_h=first_h, first_period_basis=first_basis, fit=f,
                samples=smp, rate_kind=fs.rate_kind(exchange_id),
                payment_relation_known=fs.payment_relation_known(exchange_id))


def path(lg, model, hours):
    """Hourly rate path (hours 1..H) of one leg under a model; None = unknown hour."""
    r = lg["rate_h"]
    if r is None:
        return [None] * hours, "RATE_MISSING"
    if model == "baseline":
        return [r] * hours, "PUBLISHED_RATE_CONSTANT (comparison only, not a forecast)"
    n0 = min(lg["first_period_h"], hours)
    f = lg["fit"]
    long = lg["side"] == "long"
    if f.get("status") == "OK":
        if model == "persistence":
            mu, phi, step = f["mean"], f["phi"], f["step_h"]
            tail = [mu + (r - mu) * phi ** ((k - n0 + 1) / step) for k in range(n0, hours)]
            return [r] * n0 + tail, "AR(1) on settled history (mean %.3g, phi %.3f)" % (
                mu, phi)
        q = f["resid_q90"] if long else f["resid_q10"]
        return [r + q] * hours, "published %s settled 24 h residual %s (%.3g)" % (
            "+", "q90" if long else "q10", q)
    if lg["samples"]:
        s = lg["samples"]
        if model == "persistence":
            v = statistics.mean(s)
            return [r] * n0 + [v] * (hours - n0), "current period, then the mean of %d " \
                "published samples (no settled history)" % len(s)
        v = _q(s, 0.9) if long else _q(s, 0.1)
        return [r] * n0 + [v] * (hours - n0), "current period, then %s of %d published " \
            "samples" % ("q90" if long else "q10", len(s))
    return [r] * n0 + [None] * (hours - n0), "UNKNOWN after the current period (no " \
        "settled history, no samples)"


def leg_confidence(lg):
    f = lg["fit"]
    if lg["rate_h"] is None:
        return "NONE"
    if f.get("status") == "OK":
        c = "MEDIUM" if f.get("span_h", 0) >= MEDIUM_SPAN_H else "LOW"
    elif lg["samples"]:
        c = "LOW"
    else:
        c = "NONE"
    if not lg["payment_relation_known"] and c == "MEDIUM":
        c = "LOW"
    return c


RANK = dict(NONE=0, LOW=1, MEDIUM=2)


def expect(long_leg, short_leg, notional, hours=24):
    """The three models for a long/short pair. Every cash figure is ESTIMATED.
    Flag BASELINE_ONLY_POSITIVE: the baseline funding CASH is positive while the
    persistence cash is <= 0 or UNKNOWN (a carry that rests on holding the published
    value)."""
    models = {}
    for m in MODELS:
        lp, lb = path(long_leg, m, hours)
        sp, sb = path(short_leg, m, hours)
        cash, partial = fp.pair_cash(lp, sp, notional)
        models[m] = dict(cash=cash, partial=partial,
                         carry_8h_pct=None if cash is None else cash / notional / hours * 8
                         * 100, long_basis=lb, short_basis=sb,
                         long_head=lp[:3], short_head=sp[:3])
    conf = min((leg_confidence(long_leg), leg_confidence(short_leg)), key=RANK.get)
    base = models["baseline"]["cash"]
    pers = models["persistence"]["cash"]
    flags = []
    if base is not None and base > 0 and (pers is None or pers <= 0):
        flags.append("BASELINE_ONLY_POSITIVE")
    if models["adverse"]["cash"] is None:
        flags.append("ADVERSE_UNKNOWN")
    for lg in (long_leg, short_leg):
        if not lg["payment_relation_known"]:
            flags.append("PAYMENT_RELATION_UNKNOWN:%s" % lg["exchange_id"])
    return dict(evidence_class=EVIDENCE_CLASS, notional=notional, hours=hours,
                models=models, confidence=conf, flags=flags,
                legs={lg["side"]: dict(exchange_id=lg["exchange_id"],
                                       rate_kind=lg["rate_kind"],
                                       payment_relation_known=lg["payment_relation_known"],
                                       fit_status=lg["fit"].get("status"),
                                       fit_points=lg["fit"].get("points"),
                                       samples=len(lg["samples"]),
                                       first_period_h=lg["first_period_h"],
                                       first_period_basis=lg["first_period_basis"],
                                       confidence=leg_confidence(lg))
                      for lg in (long_leg, short_leg)})


def model_nets(result, expected_net, expected_funding, realization=1.0):
    """Net after costs under each model: the entry's expected net with its baseline
    funding replaced by the model's (realization applied the same way). None when the
    model's cash is unknown or the entry has no net."""
    out = {}
    for m, x in result["models"].items():
        if x["cash"] is None or expected_net is None or expected_funding is None:
            out[m] = None
        else:
            out[m] = expected_net - expected_funding + x["cash"] * min(1.0, max(0.0,
                                                                            realization))
    return out
