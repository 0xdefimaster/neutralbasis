"""Funding persistence (Phase 14 Slice D). PAPER research, offline, no look-ahead.

Question: how long does a funding rate that is published (or settled) NOW keep its sign
and size? An entry that assumes "the published rate holds for 24 h" is the BASELINE
model; this module adds a PERSISTENCE-ADJUSTED model and an ADVERSE model, and tests
them on history in time order (fit on the first part, validate on the later part).

Inputs (rates are fractions PER HOUR, positive = longs pay):
  settled history   [(ts, rate)] from a venue's public settled funding (tradeXYZ,
                    Hyperliquid, Lighter RH, Arcus, RISEx)
  published samples [(ts, rate)] for a venue WITHOUT settled history (TxFlow,
                    Variational): only how the published value moves is measurable;
                    which value is actually PAID is UNKNOWN

Metrics per series: points, span, mean, std, sign persistence at 1/4/8/24 h, magnitude
persistence (correlation of |r|), AR(1) coefficient on the de-meaned series and its
half-life (hours), sign reversals per day.

Validation (chronological): mean and AR(1) fitted on the first `train_frac` of the
points only; for each later point t with 24 h of future data the realized average rate
over (t, t+24h] is compared with
  baseline      r_t                        (published / last rate held constant)
  persistence   mu + (r_t - mu) * mean_k phi^k      (k = 1..H)
  adverse       r_t + q10 of (realized - r_t) on the TRAIN part, in the direction
                that hurts the holder (reported as a bound and its coverage)
Also compared: trailing 24 h mean (what the engine's realized_hourly uses) and AR(1)
reverting to the trailing 7-day mean ("persistence_local"), both from points at or
before t only.
Confidence: INSUFFICIENT_HISTORY (< 72 points), LOW (span < 30 days or no validation
window), MEDIUM (validated on >= 30 days). HIGH is never given (no account payments).
"""
import math
import statistics

HOUR = 3600.0


def clean(points):
    """Sorted unique (ts, rate) with numeric rates."""
    out = {}
    for t, r in points:
        if isinstance(r, (int, float)) and r == r and t is not None:
            out[float(t)] = float(r)
    return sorted(out.items())


def ar1(xs):
    """(mean, phi) of a series; phi from the lag-1 autocorrelation of x - mean."""
    if len(xs) < 3:
        return (statistics.mean(xs) if xs else None), None
    mu = statistics.mean(xs)
    d = [x - mu for x in xs]
    den = sum(a * a for a in d)
    if den == 0:
        return mu, 1.0
    return mu, sum(a * b for a, b in zip(d, d[1:])) / den


def half_life(phi):
    if phi is None or phi <= 0 or phi >= 1:
        return None if phi is None or phi <= 0 else math.inf
    return math.log(0.5) / math.log(phi)


def corr(a, b):
    if len(a) < 3:
        return None
    ma, mb = statistics.mean(a), statistics.mean(b)
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((x - mb) ** 2 for x in b))
    if sa == 0 or sb == 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)


def metrics(series, step_s=HOUR):
    """series: clean [(ts, rate)] on (about) a regular `step_s` grid."""
    if not series:
        return dict(points=0, confidence="INSUFFICIENT_HISTORY")
    ts = [t for t, _ in series]
    rs = [r for _, r in series]
    span_h = (ts[-1] - ts[0]) / HOUR
    by_t = dict(series)

    def lagged(h):
        pairs = [(by_t[t], by_t.get(t + h * HOUR)) for t in ts]
        pairs = [(a, b) for a, b in pairs if b is not None]
        return pairs

    sign_p, mag_p = {}, {}
    for h in (1, 4, 8, 24):
        pr = lagged(h)
        nz = [(a, b) for a, b in pr if a != 0 and b != 0]
        sign_p["%dh" % h] = None if not nz else sum(1 for a, b in nz if (a > 0) == (b > 0)) / len(nz)
        mag_p["%dh" % h] = corr([abs(a) for a, _ in pr], [abs(b) for _, b in pr])
    mu, phi = ar1(rs)
    flips = sum(1 for a, b in zip(rs, rs[1:]) if a * b < 0)
    return dict(points=len(rs), span_h=span_h, mean=mu, std=statistics.pstdev(rs),
                min=min(rs), max=max(rs), sign_persistence=sign_p,
                magnitude_persistence=mag_p, ar1_phi=phi, half_life_h=half_life(phi),
                reversals_per_day=flips / (span_h / 24.0) if span_h > 0 else None,
                positive_share=sum(1 for r in rs if r > 0) / len(rs))


def validate(series, horizon_h=24, train_frac=0.7, return_rows=False):
    """Chronological fit / validate. Only data at or before t is used for t's forecast;
    the realized value uses (t, t + horizon] and is NEVER part of the fit."""
    n = len(series)
    if n < 72:
        return dict(status="INSUFFICIENT_HISTORY", points=n)
    cut = int(n * train_frac)
    train = series[:cut]
    mu, phi = ar1([r for _, r in train])
    by_t = dict(series)
    t_end = series[-1][0]

    def realized(t):
        xs = [by_t.get(t + k * HOUR) for k in range(1, horizon_h + 1)]
        return None if any(x is None for x in xs) else statistics.mean(xs)

    w = statistics.mean([phi ** k for k in range(1, horizon_h + 1)]) if phi is not None else 0
    # adverse residual quantiles from TRAIN only
    res_train = [realized(t) - r for t, r in train if t + horizon_h * HOUR <= train[-1][0]
                 and realized(t) is not None]
    if len(res_train) < 10:
        return dict(status="INSUFFICIENT_HISTORY", points=n, reason="train too short")
    q10 = sorted(res_train)[int(0.10 * (len(res_train) - 1))]
    q90 = sorted(res_train)[int(0.90 * (len(res_train) - 1))]
    rows = []
    for t, r in series[cut:]:
        if t + horizon_h * HOUR > t_end:
            break
        real = realized(t)
        if real is None:
            continue
        # trailing means use ONLY points at or before t (no look-ahead)
        t24 = [by_t[t - k * HOUR] for k in range(0, 24) if t - k * HOUR in by_t]
        t7d = [by_t[t - k * HOUR] for k in range(0, 168) if t - k * HOUR in by_t]
        mu_l = statistics.mean(t7d)
        rows.append(dict(t=t, r=r, real=real, base=r, pers=mu + (r - mu) * w,
                         trail24=statistics.mean(t24), local=mu_l + (r - mu_l) * w))
    if len(rows) < 10:
        return dict(status="INSUFFICIENT_HISTORY", points=n, reason="validation too short")
    mae = lambda k: statistics.mean(abs(x[k] - x["real"]) for x in rows)  # noqa: E731
    models = dict(baseline=mae("base"), persistence=mae("pers"), trailing_24h=mae("trail24"),
                  persistence_local=mae("local"))
    return dict(status="OK", points=n, train_points=cut, validation_points=len(rows),
                train_mean=mu, train_phi=phi, persistence_weight=w,
                mae_baseline=mae("base"), mae_persistence=mae("pers"),
                persistence_better=mae("pers") < mae("base"),
                mae=models, best_model=min(models, key=models.get),
                adverse_q10=q10, adverse_q90=q90,
                coverage_long_receives=sum(1 for x in rows if x["real"] >= x["r"] + q10)
                / len(rows),
                sign_kept_24h=sum(1 for x in rows if x["r"] != 0 and
                                  (x["real"] > 0) == (x["r"] > 0)) / len(rows),
                **(dict(rows=rows) if return_rows else {}))


def confidence(m, v):
    """How far the persistence statistics of this series can be trusted (not whether a
    trade is good). HIGH is never given: no account-payment evidence exists."""
    if m.get("points", 0) < 72:
        return "INSUFFICIENT_HISTORY"
    if v.get("status") != "OK" or m.get("span_h", 0) < 30 * 24:
        return "LOW"
    return "MEDIUM"


def leg_path(current, model, hours, fit=None, samples=None):
    """Hourly rate path for one leg over `hours` under a model.
    fit: dict(mean, phi) from settled history, or None.
    samples: published values seen (no settled history), or None."""
    if model == "baseline":
        return [current] * hours
    if fit and fit.get("phi") is not None:
        mu, phi = fit["mean"], fit["phi"]
        if model == "persistence":
            return [mu + (current - mu) * phi ** k for k in range(1, hours + 1)]
        q = fit.get("adverse")
        return [current + q] * hours if q is not None else [None] * hours
    # no settled history: the published value is trusted for ONE period only
    if not samples:
        return [current] + [None] * (hours - 1)
    if model == "persistence":
        return [current] + [statistics.mean(samples)] * (hours - 1)
    return [current] + [fit_adverse_from_samples(samples, fit)] * (hours - 1)


def fit_adverse_from_samples(samples, fit):
    side = (fit or {}).get("side", "long")
    xs = sorted(samples)
    q = xs[int(0.9 * (len(xs) - 1))] if side == "long" else xs[int(0.1 * (len(xs) - 1))]
    return q


def pair_cash(long_path, short_path, notional):
    """Hourly carry cash (short receives, long pays) over the path; None if any leg's
    rate is unknown at some hour (the result is then a range, not a number)."""
    if any(x is None for x in long_path + short_path):
        known = [(s - l) * notional for l, s in zip(long_path, short_path)
                 if l is not None and s is not None]
        return None, dict(known_hours=len(known), known_cash=sum(known))
    return sum((s - l) * notional for l, s in zip(long_path, short_path)), None
