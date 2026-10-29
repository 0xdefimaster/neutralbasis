"""Multi-venue paper desk: two strategies over every scanned perp pair.

1. Funding: carry on pairs whose advantage shows in today's rates and in the payments
   of the last 1, 3 and 7 days, with prices that moved together over the last 7 days.
   The longest window (up to 30 days) is context: a contradiction cuts the size. Profit
   is locked once a gain starts being given back; the trade leaves when carry turns.
2. Convergence: long the cheaper venue, short the dearer, equal size, when the pair's
   prices crossed regularly over both 7 and 30 days; close when they cross again.

Public data only. No order endpoint, no credentials; every fill is simulated against
the venue's real public order book. Capital is one pooled paper wallet.
"""

import collections, csv, datetime as dt, io, itertools, json, math, statistics, time, uuid
from bot import now, hour, number, walk, corr, FILL_TOLERANCE, FUNDING_UNVERIFIED_AFTER

# Native venues whose taker fee is configured; the rest come from the CCXT scanner.
NATIVE = ("Lighter", "Extended")
# Default account venues: fifteen perp venues. In per-venue mode each one has its own
# paper collateral, as a real account would: a leg is margined only by its own venue.
ACCOUNT_VENUES = (
    "Lighter",
    "Extended",
    "Hyperliquid",
    "TxFlow",
    "RISEx",
    "Arcus",
    "Binance",
    "Bybit",
    "OKX",
    "Bitget",
    "Gate",
    "Aster",
    "Paradex",
    "dYdX",
    "GRVT",
)
# Venues whose public data comes from the desk's own feed instead of the CCXT scanner.
FEED_VENUES = ("Lighter", "Extended", "TxFlow", "RISEx", "Arcus")
WIDE_DEFAULTS = dict(
    wide_enabled=True,
    wide_capital=10000.0,
    wide_size=1000.0,
    wide_leverage=3.0,
    wide_reserve=0.30,
    wide_max_positions=10,
    wide_horizon=24,
    wide_min_net=0.50,
    wide_risk=50.0,
    wide_daily_loss=500.0,
    wide_min_corr=0.90,
    wide_max_z=3.0,
    wide_deep_top=5,
    wide_cooldown_seconds=3600,
    wide_max_hold_hours=168,
    wide_funding_enabled=True,
    wide_lock_trail=0.5,
    wide_conv_enabled=True,
    wide_conv_leverage=5.0,
    wide_conv_min_cross_7=6,
    wide_conv_min_cross_30=20,
    # Days of matched candles the desk needs; 30 are used whenever they exist.
    wide_min_history_days=7,
    wide_conv_min_episodes=3,
    wide_conv_max_hold_hours=72,
    wide_conv_exit_bps=2.0,
    wide_max_spread_sd_bps=30.0,
    wide_weak_minutes=30,
)
# Earlier defaults: a stored config still holding them is moved to the current ones
# (only before any real trade, since the wallet follows the capital).
OLD_DEFAULTS = dict(
    wide_capital=1000.0,
    wide_size=500.0,
    wide_max_positions=2,
    wide_risk=10.0,
    wide_daily_loss=30.0,
    wide_deep_top=3,
)
BOOLS = ("wide_enabled", "wide_funding_enabled", "wide_conv_enabled")
LIMITS = dict(
    wide_capital=(10, 1000000),
    wide_size=(10, 100000),
    wide_leverage=(1, 5),
    wide_reserve=(0.1, 0.9),
    wide_max_positions=(1, 10),
    wide_horizon=(1, 168),
    wide_min_net=(0, 1000),
    wide_risk=(0.01, 10000),
    wide_daily_loss=(0.01, 100000),
    wide_min_corr=(0.5, 1),
    wide_max_z=(0.5, 10),
    wide_deep_top=(1, 10),
    wide_cooldown_seconds=(0, 86400),
    wide_max_hold_hours=(1, 2160),
    wide_lock_trail=(0.1, 0.9),
    wide_conv_leverage=(1, 5),
    wide_conv_min_cross_7=(1, 168),
    wide_conv_min_cross_30=(1, 720),
    wide_min_history_days=(7, 30),
    wide_conv_min_episodes=(1, 100),
    wide_conv_max_hold_hours=(1, 720),
    wide_conv_exit_bps=(0, 50),
    wide_max_spread_sd_bps=(1, 500),
    wide_weak_minutes=(0, 1440),
)
# A single leg's adverse move over the hold may use at most this share of its own
# liquidation distance; the other venue's profit does not protect it.
LEG_SAFETY = 0.5
HISTORY_TTL = 1800
# Rates for ranking may be a scan round old; entry refreshes them (ENTRY_FUNDING_MAX_AGE)
# and prices every leg on a fresh book.
ROW_MAX_AGE = 300
# Assets outside the selected list join the desk from the bulk scan when a leg has at
# least this much open interest (24h volume where OI is unpublished).
UNIVERSE_MIN_USD = 1_000_000
FUNDING_TTL = 120
MIN_NOTIONAL = 10
LATE_FUNDING_WINDOW = 7 * 86400
# A cycle later than this after the previous one counts as downtime: the desk was not
# watching, so the period's results exclude that time.
DOWNTIME_GAP = 600
# Thresholds the period report checks before results are trusted.
PERIOD_CHECKS = dict(
    days=30,
    trades=40,
    profit_factor=1.3,
    top3_share=0.5,
    downtime_share=0.05,
    paused_share=0.05,
)
# Funding quotes older than this at entry are refreshed or the entry is skipped.
ENTRY_FUNDING_MAX_AGE = 120
# Days of published funding payments a funding entry checks: the advantage must hold over
# the last 3 and 7 days without leaning on a single day.
PERSIST_DAYS = 7
# Payments may be up to 8h apart (venues move between 8h, 4h and 1h schedules); a longer
# stretch without one means the published history is incomplete.
FUNDING_GAP = 8 * 3600 + 900
# Share of the payments a leg's current schedule implies that its published history must
# hold over a window. One missing payment in twenty is tolerated (an API drops one now and
# then); fewer means the history is incomplete: a missing payment is never read as a
# smaller cost or income. A venue that moved to a shorter schedule inside the window also
# falls short until the window holds the new schedule.
PAYMENT_COVERAGE = 0.95
# The longest window is context, not a gate: each contradiction with the short-term
# signal halves the entry size.
CONTEXT_DAYS = 30
CONTEXT_CUT = 0.5


def next_payment(row, after):
    """First payment strictly after `after` on the venue's own schedule, or None."""
    iv, nxt = row.get("interval"), row.get("next")
    if not iv or not nxt:
        return None
    if nxt <= after:
        nxt += math.ceil((after - nxt) / iv + 1e-9) * iv
    return nxt


def payments_within(row, start, end):
    """Count of the venue's payments in (start, end]; whole intervals when the phase is unknown."""
    iv = row.get("interval")
    if not iv:
        return None
    first = next_payment(row, start)
    if first is None:
        return int((end - start) // iv)
    return 0 if first > end else int((end - first) // iv) + 1


def rank(rows, cfg, at):
    """Every cross-venue perp pair with positive funding carry, best first.

    Score per $1 notional over the horizon: funding each venue actually pays in that
    window (its own interval and next payment time) minus four taker fees. Price
    spread is shown but not credited here; the deep check charges an adverse one.
    """
    end = at + cfg["wide_horizon"] * 3600
    out = []
    for a, by_venue in usable_rows(rows, at).items():
        for x, y in itertools.combinations(by_venue.values(), 2):
            l, s = sorted((x, y), key=lambda r: (r["rate"], r["venue"]))
            if s["rate"] - l["rate"] <= 0:
                continue
            nl, ns = payments_within(l, at, end), payments_within(s, at, end)
            funding = s["raw_rate"] * ns - l["raw_rate"] * nl
            fees = 2 * (l["fee"] + s["fee"])
            out.append(
                dict(
                    pair_fields(a, l, s),
                    strategy="funding",
                    funding_gap=s["rate"] - l["rate"],
                    funding_horizon=funding,
                    fee_roundtrip=fees,
                    spread=math.log(s["mark"] / l["mark"]),
                    score=funding - fees,
                    payment_counts={l["venue"]: nl, s["venue"]: ns},
                )
            )
    out.sort(key=lambda c: (-c["score"], c["asset"], c["long"], c["short"]))
    for i, c in enumerate(out):
        c["rank"] = i + 1
    return out


def usable_rows(rows, at, why=None):
    """One fresh perp market per venue and asset with a known taker fee and schedule;
    `why` (a dict) counts the markets left out, by reason."""
    groups = {}
    for r in rows:
        if r.get("kind", "perp") != "perp":
            continue
        if r.get("rate") is None or r.get("raw_rate") is None:
            reason = "Funding oranı yok"
        elif not r.get("interval"):
            reason = "Funding aralığı yok"
        elif r.get("fee") is None:
            reason = "Taker ücreti bilinmiyor"
        elif not (r.get("mark") or 0) > 0:
            reason = "Fiyat yok"
        elif r.get("price_type") == "last_trade":
            reason = "Yalnızca son işlem fiyatı"
        elif at - r["received"] > ROW_MAX_AGE:
            reason = "Veri eski"
        else:
            groups.setdefault(r["asset"], {}).setdefault(r["venue"], r)
            continue
        if why is not None:
            why[reason] = why.get(reason, 0) + 1
    return groups


def pair_fields(a, l, s):
    return dict(
        asset=a,
        long=l["venue"],
        short=s["venue"],
        rate_long=l["rate"],
        rate_short=s["rate"],
        raw_long=l["raw_rate"],
        raw_short=s["raw_rate"],
        interval_long=l["interval"],
        interval_short=s["interval"],
        next_long=l.get("next"),
        next_short=s.get("next"),
        fee_long=l["fee"],
        fee_short=s["fee"],
        leverage_long=l.get("leverage"),
        leverage_short=s.get("leverage"),
        rfq_long=bool(l.get("rfq")),
        rfq_short=bool(s.get("rfq")),
        lot_long=lot_of(l),
        lot_short=lot_of(s),
    )


def venue_choices():
    """Perp venues the desk scans: the only ones an account can be simulated on."""
    from wide_markets import CATALOG

    return [v for v, c in CATALOG.items() if c["scan"] and v != "LighterCore"]


def trades_csv(trades):
    """The period's closed trades for a spreadsheet; times are UTC epoch seconds."""
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(
        [
            "id",
            "asset",
            "strategy",
            "long",
            "short",
            "opened_utc",
            "closed_utc",
            "size_usd",
            "price_pnl_usd",
            "funding_usd",
            "estimated_funding_usd",
            "fees_usd",
            "net_usd",
            "pending_funding",
            "entry_reason",
            "close_reason",
            "funding_unverified_hours",
            "fill_ratio",
            "slippage_usd",
            "unverified_risk_usd",
        ]
    )
    for x in trades:
        writer.writerow(
            [
                x.get("id"),
                x.get("asset"),
                x.get("strategy"),
                x.get("long"),
                x.get("short"),
                x.get("opened"),
                x.get("closed_at"),
                x.get("size"),
                x.get("price_pnl"),
                x.get("funding"),
                x.get("estimated_funding"),
                x.get("fees"),
                x.get("net"),
                x.get("pending"),
                x.get("entry_reason"),
                x.get("reason"),
                x.get("unverified"),
                x.get("fill_ratio", 1.0),
                x.get("slippage"),
                x.get("unverified_risk"),
            ]
        )
    return out.getvalue()


def lot_of(row):
    """A venue's order size rules, in base units and quote currency; None if unpublished."""
    return dict(
        step=row.get("step"), minimum=row.get("minimum"), min_quote=row.get("min_quote")
    )


def fit_lots(qty, price, lots):
    """Round an equal base quantity down to every leg's size step and check each venue's
    minimum order. A quantity no venue would accept is refused, never assumed."""
    steps = [x["step"] for x in lots if x and x.get("step")]
    if steps:
        big = max(steps)
        qty = math.floor(qty / big * (1 + 1e-9)) * big
        for step in steps:
            if abs(qty / step - round(qty / step)) > 1e-6:
                raise ValueError("Bacakların emir adımı uyumsuz; eşit miktar kurulamaz")
    for x in lots:
        if not x:
            continue
        if qty <= 0 or qty < (x.get("minimum") or 0) - 1e-12:
            raise ValueError("Borsanın asgari emir miktarının altında")
        if qty * price < (x.get("min_quote") or 0):
            raise ValueError("Borsanın asgari emir tutarının altında")
    return qty


def lot_floor(qty, price, lots):
    """The largest quantity at or below qty that every leg's order rules accept; 0 when
    none does (below a venue's minimum)."""
    try:
        return fit_lots(qty, price, lots) if qty > 0 else 0.0
    except ValueError:
        return 0.0


def ioc(levels, qty, buy, limit, extra=0.0):
    """An immediate-or-cancel order at `limit` against the visible book: the quantity
    filled (up to qty) at prices no worse than the limit, and its average price with
    the extra slippage. Nothing filled returns (0, None); the rest is cancelled."""
    remain, total = qty, 0.0
    for p, q in sorted(levels, reverse=not buy):
        if (p > limit) if buy else (p < limit):
            break
        take = min(remain, q)
        total += take * p
        remain -= take
        if remain < 1e-10:
            break
    filled = qty - max(remain, 0.0) if remain >= 1e-10 else qty
    if filled <= 1e-12:
        return 0.0, None
    return filled, total / filled * (1 + extra if buy else 1 - extra)


def round_counts(outcomes):
    """Pairs a round deep-checked, by final outcome; opened and skipped pairs were
    eligible, a pair rejected at entry (stale funding) was not."""
    final = list(outcomes.values())
    return dict(
        examined=len(final),
        eligible=sum(o in ("eligible", "opened", "skipped") for o in final),
        opened=final.count("opened"),
        skipped=final.count("skipped"),
        rejected=final.count("rejected"),
    )


def mid_of(book):
    return (max(p for p, q in book["bids"]) + min(p for p, q in book["asks"])) / 2


# Adverse assumptions the period's closed trades are run again under: a sound result
# survives them, not only the fills and funding the paper happened to get.
STRESS = (
    ("Kayma 2 katı", dict(slippage=1.0)),
    ("Ücret +%50", dict(fees=0.5)),
    ("Funding geliri −%25", dict(funding=0.25)),
    ("Tahmini değerlenen funding sayılmaz", dict(estimated=1.0)),
    ("Doğrulanmamış ödemeler aleyhte", dict(unverified=1.0)),
    (
        "Hepsi birlikte",
        dict(slippage=1.0, fees=0.5, funding=0.25, estimated=1.0, unverified=1.0),
    ),
)


def stressed(
    trades, slippage=0.0, fees=0.0, funding=0.0, estimated=0.0, unverified=0.0
):
    """Net of closed trades with extra slippage and fees (shares of what each trade
    paid), funding income cut by a share, estimated funding valuations removed, and
    every unpublished payment charged as the cost it would have been on a paying leg.
    A trade without a recorded slippage adds none."""
    total = 0.0
    for x in trades:
        est = estimated * max(x.get("estimated_funding") or 0.0, 0.0)
        total += (
            x["net"]
            - slippage * (x.get("slippage") or 0.0)
            - fees * x["fees"]
            - est
            - funding * max(x["funding"] - est, 0.0)
            + unverified * (x.get("unverified_risk") or 0.0)
        )
    return total


def scorecard(trades):
    """Per strategy and per venue pair: trades, net, average, winners, the worst trade
    and price result, fees against gross income (funding + price before fees), average
    hold, the deepest fall of the cumulative net from its high, slippage paid, and data
    reliability (unverified payments, share of funding valued on an estimate)."""
    groups = {}
    for x in sorted(trades, key=lambda x: x.get("closed_at") or 0):
        s = x.get("strategy") or "funding"
        for key in ((s, None, None), (s, x.get("long"), x.get("short"))):
            groups.setdefault(key, []).append(x)
    out = []
    for (s, lv, sv), xs in groups.items():
        nets = [x["net"] for x in xs]
        run = peak = fall = 0.0
        for n in nets:
            run += n
            peak = max(peak, run)
            fall = max(fall, peak - run)
        gross = sum(x["funding"] + x["price_pnl"] for x in xs)
        income = sum(max(x["funding"], 0.0) for x in xs)
        estimated = sum(max(x.get("estimated_funding") or 0.0, 0.0) for x in xs)
        holds = [
            (x["closed_at"] - x["opened"]) / 3600
            for x in xs
            if x.get("closed_at") is not None and x.get("opened") is not None
        ]
        slips = [x["slippage"] for x in xs if x.get("slippage") is not None]
        out.append(
            dict(
                strategy=s,
                long=lv,
                short=sv,
                pair=lv is not None or sv is not None,
                trades=len(xs),
                net=sum(nets),
                average=sum(nets) / len(xs),
                wins=sum(n > 0 for n in nets),
                worst=min(nets),
                worst_price=min(x["price_pnl"] for x in xs),
                fees=sum(x["fees"] for x in xs),
                fee_share=sum(x["fees"] for x in xs) / gross if gross > 0 else None,
                hold_hours=statistics.mean(holds) if holds else None,
                drawdown=fall,
                slippage=sum(slips) if slips else None,
                unverified=sum(x.get("unverified") or 0 for x in xs),
                estimated_share=estimated / income if income > 0 else None,
            )
        )
    whole = [x for x in out if not x["pair"]]
    pairs = sorted(
        (x for x in out if x["pair"]), key=lambda x: (-x["trades"], -abs(x["net"]))
    )
    return whole + pairs[:30]


# Decision amounts that scale with the filled quantity when an entry fills partly.
SIZED_FIELDS = (
    "qty",
    "size",
    "entry_fee",
    "exit_fee",
    "execution_drag",
    "stress_loss",
    "net",
    "conservative_funding",
    "funding_day1",
    "funding_day3",
    "risk_reserve",
    "reversion_cost",
    "entry_capture",
    "exit_drag_proxy",
    "funding_cost_reserve",
)

RFQ_UNSUPPORTED = "RFQ yürütmesi henüz desteklenmiyor"


def split_rfq(ranked):
    """Separate pairs with an RFQ leg before any deep check.

    An RFQ market's public book is indicative, so it cannot be priced like a normal
    order book. Those pairs go to their own review queue, labelled, and never take a
    deep-check slot or block the asset's other venue combinations.
    """
    normal, rfq = [], []
    for c in ranked:
        if c.get("rfq_long") or c.get("rfq_short"):
            rfq.append(dict(c, eligible=False, reasons=[RFQ_UNSUPPORTED], net=None))
        else:
            normal.append(c)
    return normal, rfq


def rank_spreads(rows, at):
    """Every cross-venue pair: long the cheaper mark, short the dearer, widest net gap first.

    Score: current log price gap minus four taker fees. History decides eligibility.
    """
    out = []
    for a, by_venue in usable_rows(rows, at).items():
        for x, y in itertools.combinations(by_venue.values(), 2):
            l, s = sorted((x, y), key=lambda r: (r["mark"], r["venue"]))
            gap = math.log(s["mark"] / l["mark"])
            fees = 2 * (l["fee"] + s["fee"])
            out.append(
                dict(
                    pair_fields(a, l, s),
                    strategy="convergence",
                    spread=gap,
                    fee_roundtrip=fees,
                    funding_gap=s["rate"] - l["rate"],
                    score=gap - fees,
                )
            )
    out.sort(key=lambda c: (-c["score"], c["asset"], c["long"], c["short"]))
    for i, c in enumerate(out):
        c["rank"] = i + 1
    return out


def matched(left, right, at, hours):
    """Matched closed 1h trade closes over the last `hours`; never forward-filled."""
    if left.get("price_type") != "trade" or right.get("price_type") != "trade":
        raise ValueError("Gerçek işlem mumu yok; oracle/eksik seri ile giriş yapılmaz")
    end = hour(at)
    times = [
        t
        for t in range(end - hours * 3600, end, 3600)
        if t in left["prices"] and t in right["prices"]
    ]
    if len(times) < hours * 0.95:
        raise ValueError(
            "%d günlük eşlenmiş 1h mum eksik (%d/%d saat)"
            % (hours // 24, len(times), hours)
        )
    return times


def history_days(left, right, at, minimum=30):
    """Longest whole-day window, from 30 down to `minimum`, with matched candles."""
    matched(left, right, at, minimum * 24)
    for days in range(30, minimum, -1):
        try:
            matched(left, right, at, days * 24)
            return days
        except ValueError:
            continue
    return minimum


def pair_stats(left, right, at, horizon, minimum=30):
    """Hedge quality on matched closed 1h trade candles over 7 days and the longest
    window available up to 30 days (reported as corr30 / history_days)."""
    long = history_days(left, right, at, minimum)
    out = dict(history_days=long)
    for days in (long, 7):
        times = matched(left, right, at, days * 24)
        a = [left["prices"][t] for t in times]
        b = [right["prices"][t] for t in times]
        ra = [math.log(a[i] / a[i - 1]) for i in range(1, len(a))]
        rb = [math.log(b[i] / b[i - 1]) for i in range(1, len(b))]
        c, beta = corr(ra, rb)
        if days == long:
            out["corr30"] = c
        if days != 7:
            continue
        out["corr"] = c
        spread = [math.log(y / x) for x, y in zip(a, b)]
        mean, sd = statistics.mean(spread), statistics.pstdev(spread)
        residual = statistics.pstdev([x - y for x, y in zip(ra, rb)])
        out.update(
            vol=max(statistics.pstdev(ra), statistics.pstdev(rb)),
            beta=beta,
            mean=mean,
            sd=sd,
            z=(spread[-1] - mean) / sd if sd > 1e-10 else 0.0,
            stress_fraction=3 * residual * math.sqrt(horizon),
            points=len(times),
        )
    return out


def crossings(series, deadband):
    """Sign changes of the price gap outside a small deadband, and two-sided days."""
    sign = count = 0
    days = {}
    for t, x in series:
        current = 1 if x > deadband else -1 if x < -deadband else 0
        if current and sign and current != sign:
            count += 1
        if current:
            sign = current
        lo, hi = days.get(t // 86400, (0.0, 0.0))
        days[t // 86400] = (min(lo, x), max(hi, x))
    two_sided = sum(lo < -deadband and hi > deadband for lo, hi in days.values())
    return count, two_sided


def closing_times(series, level, target):
    """Hours each past excursion to `level` took to come back to `target`."""
    out = []
    start = None
    for t, x in series:
        if start is None and x >= level:
            start = t
        elif start is not None and x <= target:
            out.append((t - start) / 3600)
            start = None
    return out


def convergence_stats(left, right, at, gap, cfg):
    """Regular, stable crossings of the pair's normal gap over both 7 and 30 days, and
    the usual time to return to it.

    Gap is log(short venue / long venue): the short venue is the dearer one now. The
    normal gap (baseline) is the median over the window, so two venues that always
    differ a little (0.99 vs 0.98) still revert: crossings, closing times and the
    target are measured around the baseline instead of zero.
    """
    days = history_days(left, right, at, cfg.get("wide_min_history_days", 30))
    times = matched(left, right, at, days * 24)
    series = [(t, math.log(right["prices"][t] / left["prices"][t])) for t in times]
    week = [x for x in series if x[0] >= hour(at) - 7 * 86400]
    xs30 = [x for t, x in series]
    xs7 = [x for t, x in week]
    mean30, sd30 = statistics.mean(xs30), statistics.pstdev(xs30)
    mean7, sd7 = statistics.mean(xs7), statistics.pstdev(xs7)
    base30, base7 = statistics.median(xs30), statistics.median(xs7)
    dev = [(t, x - base30) for t, x in series]
    deadband = max(0.00002, sd30 * 0.15)
    cross7, days7 = crossings([x for x in dev if x[0] >= week[0][0]], deadband)
    cross30, days30 = crossings(dev, deadband)
    target = cfg["wide_conv_exit_bps"] / 10000
    level = max((gap - base30) * 0.5, target + 0.0003)
    closes = closing_times(dev, level, target)
    return dict(
        baseline=base30,
        baseline7=base7,
        cross7=cross7,
        cross30=cross30,
        two_sided7=days7,
        two_sided30=days30,
        mean7=mean7,
        sd7=sd7,
        mean30=mean30,
        sd30=sd30,
        history_days=days,
        episodes=len(closes),
        avg_close_hours=statistics.mean(closes) if closes else None,
        max_close_hours=max(closes) if closes else None,
        level=level,
    )


def realized_raw(rows, interval, at):
    """Conservative per-payment rate from the last 24h: None if payments are missing."""
    recent = [r["rate"] for r in rows if at - 86400 < r["ts"] <= at]
    if len(recent) < max(1, int(86400 / interval) - 1):
        return None
    return statistics.mean(recent)


def covered_since(rows, start, end, gap):
    """Earliest time from which (t, end] has no stretch longer than `gap` without a
    published payment: `start` when the whole window is covered, `end` when even the
    latest payment is missing."""
    ts = sorted({r["ts"] for r in rows if start < r["ts"] <= end}, reverse=True)
    if not ts or end - ts[0] > gap:
        return end
    since = ts[0]
    for t in ts[1:]:
        if since - t > gap:
            break
        since = t
    return start if since - start <= gap else since


def payment_shortfall(rows, start, end, interval):
    """None when (start, end] holds at least PAYMENT_COVERAGE of the payments the venue's
    interval implies and no stretch longer than the allowed gap without one; otherwise
    (payments held, payments expected)."""
    have = sum(start < r["ts"] <= end for r in rows)
    expected = int((end - start) // interval) if interval else 0
    gap = max(FUNDING_GAP, (interval or 0) + 900)
    if (
        have >= expected * PAYMENT_COVERAGE
        and covered_since(rows, start, end, gap) <= start
    ):
        return None
    return have, expected


def daily_carry(long_rows, short_rows, at, days):
    """Realized funding of long/short per $1 notional for each of the last `days` days
    (index 0 = the last 24h): what the short leg received minus what the long leg paid,
    summed over the payments each venue published."""
    out = [0.0] * days
    for rows, sign in ((short_rows, 1), (long_rows, -1)):
        for r in rows:
            age = at - r["ts"]
            if 0 <= age < days * 86400:
                out[int(age // 86400)] += sign * r["rate"]
    return out


def usd(x):
    return ("-" if x < 0 else "+" if x > 0 else "") + "$%.2f" % abs(x)


def universe_rows(universe, rows, held=()):
    """Bulk-scan markets for every asset, as desk rows. Markets already covered by the
    native feed or the selected scan are skipped; a leg must carry UNIVERSE_MIN_USD of
    open interest (or volume) unless the desk already holds that asset."""
    have = {(r["venue"], r["asset"]) for r in rows}
    out = []
    for v, entries in universe.items():
        if v in NATIVE:
            continue
        for a, x in entries.items():
            if (v, a) in have:
                continue
            depth = x.get("oi") if x.get("oi") is not None else x.get("volume")
            if a not in held and (depth or 0) < UNIVERSE_MIN_USD:
                continue
            out.append(
                dict(
                    asset=a,
                    venue=v,
                    kind="perp",
                    symbol=x["symbol"],
                    mark=x.get("mark"),
                    price_type="mark",
                    rate=x["rate"],
                    raw_rate=x["raw_rate"],
                    interval=x["interval"],
                    next=x.get("next"),
                    received=x["received"],
                    funding_received=x["received"],
                    fee=x.get("fee"),
                    leverage=x.get("leverage"),
                    volume=x.get("volume"),
                    oi=x.get("oi"),
                    execution=False,
                    step=x.get("step"),
                    minimum=x.get("minimum"),
                    min_quote=x.get("min_quote"),
                )
            )
    return out


class EngineSource:
    """Routes market data to the native feed or the CCXT scanner."""

    def __init__(self, engine):
        self.engine = engine

    def market_rows(self):
        e = self.engine
        rows = []
        self.excluded = {}
        for v in FEED_VENUES:
            try:
                fee = e.fee(v)
            except ValueError as ex:
                # Listed without a fee: ranking skips it until the user enters one.
                fee = None
                if e.feed.meta.get(v):
                    self.excluded[v] = str(ex)
            for a, m in list(e.feed.meta.get(v, {}).items()):
                if not m or m.get("rate") is None:
                    continue
                interval = m.get("interval") or 3600
                rows.append(
                    dict(
                        asset=a,
                        venue=v,
                        kind="perp",
                        mark=m["mark"],
                        rate=m["rate"],
                        raw_rate=m.get("raw_rate", m["rate"] * interval / 3600),
                        interval=interval,
                        next=m.get("next"),
                        received=m["received"],
                        fee=fee,
                        leverage=m.get("max_leverage"),
                        price_type="mark",
                        rfq=bool(m.get("rfq")),
                        step=m.get("step"),
                        minimum=m.get("minimum"),
                        min_quote=m.get("min_quote"),
                    )
                )
        extra, _ = e.wide.snapshot()
        rows += [
            r for r in extra if r["kind"] == "perp" and r["venue"] not in FEED_VENUES
        ]
        held = {
            p["asset"] for p in getattr(getattr(e, "wide_paper", None), "positions", [])
        }
        return rows + universe_rows(e.wide.universe_snapshot(), rows, held)

    def book(self, v, a):
        if v in FEED_VENUES:
            return self.engine.feed.book(v, a)
        return self.engine.wide.book(v, a)

    def prices(self, v, a):
        keeper = getattr(self.engine, "history", None)
        if keeper is not None:
            from desk_analytics import DeskHistorySource

            stored = DeskHistorySource(self.engine).stored_prices(v, a)
            if stored:
                return stored
        try:
            if v in NATIVE:
                live = {**self.engine.feed.candles(v, a), "price_type": "trade"}
            elif v in FEED_VENUES:
                # Oracle-priced candles stay labelled; history checks refuse them.
                live = self.engine.feed.candles(v, a)
            else:
                live, _, _ = self.engine.wide.history(v, a, 30)
        except Exception as ex:
            live, error = None, ex
        # A venue with a short public history still has what the keeper recorded.
        kept = (
            keeper.db.candles(v, a, "perp", hour(now()) - 720 * 3600)
            if keeper is not None
            else {}
        )
        if live is None:
            if not kept:
                raise error
            return dict(prices=kept, price_type="trade", audit_ids=[], end=hour(now()))
        if kept and live.get("price_type") == "trade":
            live = dict(live, prices={**kept, **live["prices"]})
        return live

    def current_funding(self, v, a):
        if v in FEED_VENUES:
            raise ValueError(v + ": yerel besleme kendi tazeliğini tutar")
        return self.engine.wide.current_funding(v, a)

    def funding(self, v, a, start):
        if v in FEED_VENUES:
            return self.engine.feed.funding_history(v, a, start)
        return self.engine.wide.funding_since(v, a, start)

    def stored_funding(self, v, a, start):
        """Payments the background history keeper recorded (up to 30 days)."""
        keeper = getattr(self.engine, "history", None)
        return keeper.db.funding(v, a, start) if keeper is not None else []


# A rejected pair waits this long before it is deep-checked again, so the next ranked
# pairs get their turn; missing history is retried sooner since the keeper fills it.
REJECT_COOLDOWN = 1800
HISTORY_RETRY = 900
HISTORY_MARKERS = ("mum eksik", "işlem mumu yok", "geçmişi eksik")
# Reasons measured over days change slowly: such a pair waits longer, so its deep-check
# slot goes to pairs further down the list. A pair waits for its slowest reason, since
# it cannot qualify before every reason clears.
SLOW_REASONS = {
    "3 günde gerçekleşen funding bu yönde avantaj göstermiyor": 3 * 3600,
    "7 günde gerçekleşen funding bu yönde avantaj göstermiyor": 6 * 3600,
    "Avantaj tek bir güne dayanıyor": 6 * 3600,
    "7g getiri korelasyonu sınır altında": 6 * 3600,
    "7g / 30g getiri korelasyonu sınır altında": 6 * 3600,
    "Hedge beta uyumsuz": 6 * 3600,
    "Normal fiyat farkı kayıyor": 3 * 3600,
}


def reason_wait(group):
    """How long a pair rejected for this kind of reason waits (keys match as prefixes,
    since some reasons name their window)."""
    return next(
        (t for k, t in SLOW_REASONS.items() if group.startswith(k)), REJECT_COOLDOWN
    )


# Deep checks are counted per pair over this window for the funnel.
CHECKED_WINDOW = 3600


def reason_group(reason):
    """A rejection's kind; measured values follow " · " and stay out of the group."""
    if any(m in reason for m in HISTORY_MARKERS):
        return "Geçmiş eksik"
    return reason.split(" · ")[0]


class WidePaper:
    def __init__(self, store, source, sleep=time.sleep):
        self.store = store
        self.source = source
        self.sleep = sleep
        self.cfg = {**WIDE_DEFAULTS, **store.get("wide_config", {})}
        self.positions = store.get("wide_positions", [])
        self.closed = store.get("wide_closed", [])
        self.wallet = store.get("wide_wallet", {"cash": self.cfg["wide_capital"]})
        # Taker fees the user read from their own account, by venue; public otherwise.
        self.fees = store.get("wide_fees", {})
        if not self.traded():
            moved = self.moved_defaults()
            if moved:
                self.cfg.update(moved)
                self.wallet = {"cash": self.cfg["wide_capital"]}
        self.manual_close = set()
        self.candidates = []
        self.assessed = []
        self.conv_candidates = []
        self.rfq_review = []
        self.rejected = {}
        # The latest decision per pair, kept past the cooldown for the pair pages.
        self.decisions = {}
        self.review = dict(
            rounds=0, examined=0, eligible=0, skipped=0, opened=0, reasons={}
        )
        # Last deep check per pair, and each pair's final outcome in the latest round
        # ({strategy: {pair: outcome}}), for the funnel.
        self.checked = {}
        self.round = {}
        self.funnel = None
        self.rows = {}
        self.last_cycle = None
        self.error = None
        self.cache = {}
        self.latency_ms = 750
        self.extra_slippage = 0.0001
        # Measured round trips of each venue's public book request, in seconds.
        self.rtt = collections.defaultdict(lambda: collections.deque(maxlen=50))

    # --- state ---------------------------------------------------------------
    def persist(self):
        with self.store.lock:
            for k, v in (
                ("wide_positions", self.positions),
                ("wide_closed", self.closed[-500:]),
                ("wide_wallet", self.wallet),
                ("wide_config", self.cfg),
            ):
                self.store.db.execute(
                    "insert or replace into kv values(?,?)", (k, json.dumps(v))
                )
            self.store.db.commit()

    def events(self, kind, message, **kwargs):
        self.store.event("WIDE_" + kind, dict(message=message, **kwargs))

    def moved_defaults(self):
        """Settings still at an earlier default; a desk without trades, or a new period,
        takes the current ones. Values the user changed stay."""
        return {
            k: WIDE_DEFAULTS[k]
            for k, old in OLD_DEFAULTS.items()
            if self.cfg.get(k) == old
        }

    def per_venue(self):
        """Each account venue holds its own collateral (set by a reset)."""
        return "venues" in self.wallet

    def credit(self, venue, amount):
        self.wallet["cash"] += amount
        if self.per_venue():
            self.wallet["venues"][venue] = (
                self.wallet["venues"].get(venue, 0.0) + amount
            )

    def balance(self, venue=None):
        legs = [
            leg
            for p in self.positions
            for leg in p["legs"]
            if not leg["closed"] and (venue is None or leg["venue"] == venue)
        ]
        pnl = sum(leg.get("price_pnl", 0) for leg in legs)
        margin = sum(leg["margin"] for leg in legs)
        cash = (
            self.wallet["cash"]
            if venue is None
            else self.wallet.get("venues", {}).get(venue, 0.0)
        )
        equity = cash + pnl
        return dict(cash=cash, equity=equity, margin=margin, free=equity - margin)

    def headroom(self, venue=None):
        """Free collateral after the reserve; per venue in per-venue mode, where a
        venue outside the account list has none."""
        if venue is not None and not self.per_venue():
            venue = None
        if venue is not None and venue not in self.wallet["venues"]:
            return 0.0
        b = self.balance(venue)
        return max(0, b["free"] - max(0, b["equity"]) * self.cfg["wide_reserve"])

    def venue_balances(self):
        if not self.per_venue():
            return []
        fees = {}
        for (v, a), r in self.rows.items():
            fees.setdefault(v, [])
            if r.get("fee") is not None:
                fees[v].append(r["fee"])
        excluded = getattr(self.source, "excluded", {})
        out = []
        for v in self.wallet["venues"]:
            known = fees.get(v, [])
            if known:
                reason = None
            elif v in fees:
                reason = excluded.get(v) or "Taker ücreti bilinmiyor"
            else:
                reason = "Piyasa verisi yok"
            out.append(
                dict(
                    venue=v,
                    **self.balance(v),
                    headroom=self.headroom(v),
                    data=bool(known),
                    reason=reason,
                    fee=max(set(known), key=known.count) if known else None,
                    fee_source=(
                        "user" if v in self.fees else ("public" if known else None)
                    ),
                    latency_ms=round(self.latency(v) * 1000),
                    measured=len(self.rtt[v]),
                )
            )
        return out

    def daily_pnl(self):
        day = (
            dt.datetime.fromtimestamp(now(), dt.timezone(dt.timedelta(hours=3)))
            .date()
            .isoformat()
        )
        equity = self.wallet["cash"] + sum(
            p.get("unrealized", 0) for p in self.positions
        )
        base = self.store.get("wide_daily", {})
        if base.get("day") != day:
            base = {"day": day, "equity": equity}
            self.store.put("wide_daily", base)
        return equity - base["equity"]

    def update_config(self, changes):
        fresh = dict(self.cfg)
        for k, v in changes.items():
            if k in BOOLS:
                if not isinstance(v, bool):
                    raise ValueError(k + " true/false olmalı")
                fresh[k] = v
                continue
            if k not in LIMITS:
                raise ValueError("Bilinmeyen ayar")
            n = number(v)
            lo, hi = LIMITS[k]
            if not lo <= n <= hi:
                raise ValueError(k + " sınır dışında")
            fresh[k] = int(n) if isinstance(WIDE_DEFAULTS[k], int) else n
        if fresh["wide_capital"] != self.cfg["wide_capital"]:
            if self.per_venue():
                raise ValueError(
                    "Borsa başı teminat yalnızca Arşivle ve sıfırla ile değişir"
                )
            if self.traded():
                raise ValueError(
                    "Çoklu borsa sermayesi işlem kayıtlarından sonra değiştirilemez"
                )
            self.wallet = {"cash": fresh["wide_capital"]}
            self.store.put("wide_daily", {})
        changed = fresh != self.cfg
        self.cfg = fresh
        self.persist()
        if changed:
            self.note_change()
        self.events("CONFIG", "Çoklu borsa paper ayarları güncellendi")

    def set_fees(self, changes):
        """Taker fee per venue as a fraction (0.00045 = %0.045), read from the user's own
        account page. None removes an entry and the public tariff applies again."""
        choices = venue_choices()
        fresh = dict(self.fees)
        for v, fee in changes.items():
            if v not in choices:
                raise ValueError("Taranmayan borsa: " + str(v))
            if fee is None:
                fresh.pop(v, None)
                continue
            n = number(fee)
            if not 0 <= n <= 0.01:
                raise ValueError(v + " ücreti %0 ile %1 arasında olmalı")
            fresh[v] = n
        if fresh == self.fees:
            return
        self.fees = fresh
        self.store.put("wide_fees", fresh)
        self.note_change()
        self.events("FEES", "Borsa ücretleri güncellendi", fees=fresh)

    def with_fees(self, rows):
        return [
            (
                dict(r, fee=self.fees[r["venue"]], fee_source="user")
                if r["venue"] in self.fees
                else r
            )
            for r in rows
        ]

    def mark_epoch(self, at):
        """A period without a reset starts at its first recorded cycle or change."""
        if self.store.get("wide_epoch_start", None) is None:
            self.store.put("wide_epoch_start", at)

    def note_change(self):
        """A settings change inside a period: its results mix two configurations."""
        self.mark_epoch(now())
        run = self.store.get("wide_run", {})
        run.setdefault("changes", []).append(now())
        self.store.put("wide_run", run)

    def reset(self, capital=None, venue_capital=None, venues=ACCOUNT_VENUES):
        """Start a new paper period: archive closed trades and their results, then begin
        again from fresh capital. Results of different capitals are never mixed. With
        venue_capital every account venue starts with that much of its own collateral.
        """
        if self.positions:
            raise ValueError("Açık pozisyonlar kapanmadan paper sıfırlanamaz")
        lo, hi = LIMITS["wide_capital"]
        if venue_capital is not None:
            venue_capital = number(venue_capital)
            venues = list(dict.fromkeys(venues or ACCOUNT_VENUES))
            unknown = [v for v in venues if v not in venue_choices()]
            if unknown:
                raise ValueError("Taranmayan borsa: " + ", ".join(map(str, unknown)))
            if len(venues) < 2:
                raise ValueError("En az iki borsa gerekli")
            if not lo <= venue_capital <= hi:
                raise ValueError("Borsa başı teminat sınır dışında")
            capital = venue_capital * len(venues)
        else:
            capital = (
                WIDE_DEFAULTS["wide_capital"] if capital is None else number(capital)
            )
        if not lo <= capital <= hi:
            raise ValueError("wide_capital sınır dışında")
        trades = self.period_trades()
        epochs = self.store.get("wide_epochs", [])
        epochs.append(
            dict(
                started=self.store.get("wide_epoch_start", None),
                ended=now(),
                capital=self.cfg["wide_capital"],
                cash=self.wallet["cash"],
                venues=self.wallet.get("venues"),
                trades=len(trades),
                net=sum(p["net"] for p in trades),
                report=self.report(),
                period=self.period_report(),
                closed=self.closed,
            )
        )
        self.store.put("wide_epochs", epochs)
        self.store.put("wide_epoch_start", now())
        self.store.put("wide_run", {})
        self.store.put("wide_equity", [])
        self.store.put("wide_ledger", {})
        self.closed = []
        self.cfg.update(self.moved_defaults())
        self.cfg["wide_capital"] = capital
        self.wallet = {"cash": capital}
        if venue_capital is not None:
            self.wallet["venues"] = {v: venue_capital for v in venues}
        self.store.put("wide_daily", {})
        self.persist()
        self.events(
            "RESET",
            "Paper yeni dönemle sıfırlandı; önceki dönem arşivlendi",
            capital=capital,
            venue_capital=venue_capital,
            archived=len(trades),
        )

    def epochs(self):
        return [
            {k: v for k, v in e.items() if k != "closed"}
            for e in self.store.get("wide_epochs", [])
        ]

    def traded(self):
        """Any real (filled) position, open or closed."""
        return bool(self.positions) or any(p.get("legs") for p in self.closed)

    # --- cached public reads ---------------------------------------------------
    def cached(self, key, ttl, fn):
        hit = self.cache.get(key)
        if hit and now() - hit[0] < ttl:
            return hit[1]
        value = fn()
        self.cache[key] = (now(), value)
        return value

    def funding_rows(self, v, a, start):
        return self.cached(
            ("funding", v, a, hour(start)),
            FUNDING_TTL,
            lambda: self.source.funding(v, a, start),
        )

    def funding_window(self, v, a, at, days):
        """Published payments in the last `days` days: the venue's own history for the
        last week, completed by the keeper's stored record; one payment counts once."""
        start = at - days * 86400
        merged = {}
        stored = getattr(self.source, "stored_funding", None)
        if stored:
            try:
                for r in stored(v, a, start):
                    merged[round(r["ts"] / 60)] = r
            except Exception:
                pass
        try:
            live = self.funding_rows(v, a, at - min(days, PERSIST_DAYS) * 86400)
        except Exception:
            if not merged:
                raise
            live = []
        for r in live:
            merged[round(r["ts"] / 60)] = r
        return sorted(
            (r for r in merged.values() if start < r["ts"] <= at),
            key=lambda r: r["ts"],
        )

    def price_history(self, v, a):
        return self.cached(
            ("prices", v, a), HISTORY_TTL, lambda: self.source.prices(v, a)
        )

    # --- entry ---------------------------------------------------------------
    def blocked(self, c):
        cfg = self.cfg
        reasons = []
        if any(p["asset"] == c["asset"] for p in self.positions):
            reasons.append("Bu varlıkta açık çoklu borsa pozisyonu var")
        if any(
            p["asset"] == c["asset"]
            and now() - p.get("closed_at", 0) < cfg["wide_cooldown_seconds"]
            for p in self.closed
        ):
            reasons.append("Kapanış sonrası bekleme süresi")
        return reasons

    def entry_halt(self):
        """Why no new position may open now, or None."""
        busy = [p["status"] for p in self.positions if p["status"] != "OPEN"]
        if busy:
            return "açık pozisyon " + busy[0] + " durumunda"
        if len(self.positions) >= self.cfg["wide_max_positions"]:
            return "pozisyon sınırı dolu"
        if self.daily_pnl() <= -self.cfg["wide_daily_loss"]:
            return "günlük kayıp limiti doldu"
        return None

    def risk_budget(self):
        """Loss one new trade may risk: the per-trade limit or what is left of the day's."""
        return min(
            self.cfg["wide_risk"], self.cfg["wide_daily_loss"] + self.daily_pnl()
        )

    @staticmethod
    def worst_loss(c):
        return c["entry_fee"] + c["exit_fee"] + c["execution_drag"] + c["stress_loss"]

    def priced(self, c, leverage_cap, stress, size, vol=0.0, hold=24):
        """Size against margin, reserve and loss budget; VWAP all four fills on fresh books."""
        cfg = self.cfg
        a, lv, sv = c["asset"], c["long"], c["short"]
        lb, sb = self.book(lv, a), self.book(sv, a)
        if (
            abs(lb["received"] - sb["received"]) > 5
            or now() - min(lb["received"], sb["received"]) > 30
        ):
            raise ValueError("İki bacak defterleri güncel/senkron değil")
        mid = {
            v: (max(p for p, q in b["bids"]) + min(p for p, q in b["asks"])) / 2
            for v, b in ((lv, lb), (sv, sb))
        }
        # Leverage is a ceiling: each leg stands alone on its venue, so a 4-sigma single
        # leg move over the hold must stay inside half of its liquidation distance.
        move = 4 * vol * math.sqrt(max(1, hold))
        leverage = max(
            1.0,
            min(
                [leverage_cap, LEG_SAFETY / move if move > 0 else leverage_cap]
                + [x for x in (c.get("leverage_long"), c.get("leverage_short")) if x]
            ),
        )
        fee_l, fee_s = c["fee_long"], c["fee_short"]
        extra = self.extra_slippage
        if self.daily_pnl() <= -cfg["wide_daily_loss"]:
            raise ValueError("Günlük kayıp limiti doldu")
        budget = self.risk_budget()
        # Margin plus entry fee of both legs per $1 notional, at the worst accepted fill.
        # In per-venue mode each leg is funded by its own venue's free collateral.
        if self.per_venue():
            room = min(
                self.headroom(v) / ((1 + FILL_TOLERANCE) * (1 / leverage + f))
                for v, f in ((lv, fee_l), (sv, fee_s))
            )
        else:
            room = self.headroom() / (
                (1 + FILL_TOLERANCE) * (2 / leverage + fee_l + fee_s)
            )
        notional = min(
            size,
            room,
            budget / max(stress + c["fee_roundtrip"] + 4 * extra, 1e-4),
        )
        price = (mid[lv] + mid[sv]) / 2
        if notional < MIN_NOTIONAL:
            raise ValueError("Risk/marjin sınırı sonrası büyüklük minimumun altında")
        # Equal base quantity on both legs, in sizes both venues accept. The loss budget
        # must also hold the books' own bid-ask spread, which the first sizing cannot
        # see: the quantity shrinks until fees, spread and stress all fit inside it.
        for _ in range(4):
            qty = fit_lots(
                notional / price, price, [c.get("lot_long"), c.get("lot_short")]
            )
            if qty * price < MIN_NOTIONAL:
                raise ValueError(
                    "Risk/marjin sınırı sonrası büyüklük minimumun altında"
                )
            la = walk(lb["asks"], qty, True, extra)
            ss = walk(sb["bids"], qty, False, extra)
            lc = walk(lb["bids"], qty, False, extra)
            sc = walk(sb["asks"], qty, True, extra)
            worst = qty * (
                (la + lc) * fee_l
                + (ss + sc) * fee_s
                + (la - lc)
                + (sc - ss)
                + price * stress
            )
            if worst <= budget + 1e-6:
                break
            notional *= budget / worst * (1 - 1e-9)
        return dict(
            qty=qty,
            price=price,
            size=qty * price,
            leverage=leverage,
            budget=budget,
            entry_long=la,
            entry_short=ss,
            entry_fee=qty * (la * fee_l + ss * fee_s),
            exit_fee=qty * (lc * fee_l + sc * fee_s),
            execution_drag=qty * ((la - lc) + (sc - ss)),
            stress_loss=qty * price * stress,
            live_spread=math.log(mid[sv] / mid[lv]),
            audit_ids=[lb.get("audit_id"), sb.get("audit_id")],
        )

    def conservative_raw(self, c, at, required=True):
        """Long pays the higher of current/24h realized, short earns the lower."""
        raw = {}
        for v, side, iv, current in (
            (c["long"], "long", c["interval_long"], c["raw_long"]),
            (c["short"], "short", c["interval_short"], c["raw_short"]),
        ):
            try:
                realized = realized_raw(
                    self.funding_window(v, c["asset"], at, PERSIST_DAYS), iv, at
                )
            except Exception:
                realized = None
            if realized is None:
                if required:
                    raise ValueError(v + ": 24 saat gerçekleşmiş funding geçmişi eksik")
                realized = current
            raw[side] = (
                max(current, realized) if side == "long" else min(current, realized)
            )
        return raw

    def assess(self, c):
        if c.get("strategy") == "convergence":
            return self.assess_convergence(c)
        return self.assess_funding(c)

    def realized_carry(self, c, at):
        """This direction's realized funding per $1 notional for each of the last 7
        days (index 0 = the last 24h). A leg whose payments do not cover the week stops
        the check: persistence is never assumed."""
        start = at - PERSIST_DAYS * 86400
        rows = {}
        for v, iv in (
            (c["long"], c["interval_long"]),
            (c["short"], c["interval_short"]),
        ):
            r = self.funding_window(v, c["asset"], at, PERSIST_DAYS)
            short = payment_shortfall(r, start, at, iv)
            if short:
                raise ValueError(
                    "%s: 7 günlük gerçekleşmiş funding geçmişi eksik · %d/%d ödeme"
                    % ((v,) + short)
                )
            rows[v] = r
        return daily_carry(rows[c["long"]], rows[c["short"]], at, PERSIST_DAYS)

    def long_context(self, c, at, stats):
        """The longest window (up to 30 days) as context, never a rejection: notes, and a
        confidence that halves for each contradiction with the short-term signal."""
        cfg = self.cfg
        notes, confidence = [], 1.0
        days = stats["history_days"]
        out = dict(price_days=days, corr_long=stats["corr30"])
        if days > PERSIST_DAYS:
            if stats["corr30"] < cfg["wide_min_corr"]:
                confidence *= CONTEXT_CUT
                notes.append(
                    "%dg getiri korelasyonu %.3f < %.2f; büyüklük yarıya indi"
                    % (days, stats["corr30"], cfg["wide_min_corr"])
                )
        else:
            notes.append("Uzun dönem fiyat geçmişi yok (%d gün)" % days)
        start = at - CONTEXT_DAYS * 86400
        try:
            legs, since = {}, start
            for v, iv in (
                (c["long"], c["interval_long"]),
                (c["short"], c["interval_short"]),
            ):
                legs[v] = self.funding_window(v, c["asset"], at, CONTEXT_DAYS)
                since = max(
                    since,
                    covered_since(
                        legs[v], start, at, max(FUNDING_GAP, (iv or 0) + 900)
                    ),
                )
            fdays = int((at - since) // 86400)
            out["funding_days"] = fdays
            # The covered span must also hold the payments each schedule implies.
            short = [
                payment_shortfall(legs[v], at - fdays * 86400, at, iv)
                for v, iv in (
                    (c["long"], c["interval_long"]),
                    (c["short"], c["interval_short"]),
                )
            ]
            if fdays > PERSIST_DAYS and any(short):
                have, expected = next(x for x in short if x)
                notes.append(
                    "Uzun dönem funding geçmişi eksik (%dg, %d/%d ödeme)"
                    % (fdays, have, expected)
                )
            elif fdays > PERSIST_DAYS:
                total = sum(daily_carry(legs[c["long"]], legs[c["short"]], at, fdays))
                out["carry"] = total
                if total <= 0:
                    confidence *= CONTEXT_CUT
                    notes.append(
                        "%dg gerçekleşen funding bu yönün tersine; büyüklük yarıya indi"
                        % fdays
                    )
            else:
                notes.append("Uzun dönem funding geçmişi yok (%d gün)" % fdays)
        except Exception:
            notes.append("Uzun dönem funding geçmişi okunamadı")
        out.update(confidence=confidence, notes=notes)
        return out

    def assess_funding(self, c):
        """Carry on a pair whose advantage shows today and held over the last 3 and 7
        days, with prices that moved together over the last 7 days. The longest window
        (up to 30 days) is context: a contradiction cuts the size, it never rejects."""
        cfg = self.cfg
        a, lv, sv = c["asset"], c["long"], c["short"]
        out = dict(c, reasons=list(self.blocked(c)), eligible=False, net=None)
        try:
            at = now()
            stats = pair_stats(
                self.price_history(lv, a),
                self.price_history(sv, a),
                at,
                cfg["wide_horizon"],
                cfg["wide_min_history_days"],
            )
            out["stats"] = stats
            # 1 day: each leg at the worse of its current quote and its last 24h payments.
            raw = self.conservative_raw(c, at)
            # 3 and 7 days: what this direction was really paid, day by day.
            days = self.realized_carry(c, at)
            context = self.long_context(c, at, stats)
            out.update(realized_days=days, context=context)
            q = self.priced(
                c,
                cfg["wide_leverage"],
                stats["stress_fraction"],
                cfg["wide_size"] * context["confidence"],
                stats["vol"],
                cfg["wide_horizon"],
            )
            counts = c["payment_counts"]
            day1 = q["size"] * (raw["short"] * counts[sv] - raw["long"] * counts[lv])
            # The last 3 days' average over the horizon: an advantage that only appeared
            # today is not projected at today's level.
            day3 = q["size"] * sum(days[:3]) / 3 * cfg["wide_horizon"] / 24
            funding = min(day1, day3)
            # Short the rich leg: reverting to the 7d mean only costs when we enter below it.
            reversion = q["size"] * min(0.0, q["live_spread"] - stats["mean"])
            # Risk reserve: one 7d standard deviation of the price gap widening.
            risk_reserve = q["size"] * stats["sd"]
            net = (
                funding
                - q["entry_fee"]
                - q["exit_fee"]
                - q["execution_drag"]
                + reversion
                - risk_reserve
            )
            week, three = sum(days), sum(days[:3])
            out.update(
                q,
                conservative_funding=funding,
                funding_day1=day1,
                funding_day3=day3,
                conservative_raw=raw,
                reversion_cost=reversion,
                risk_reserve=risk_reserve,
                net=net,
                entry_reason=(
                    "Funding: short %s, long %s; beklenen %s saatlik funding %s "
                    "(1g %s, 3g ort. %s), gerçekleşen 3g %s / 7g %s (%d/7 gün pozitif), "
                    "korelasyon 7g %.3f / %dg %.3f, fark sd %.1f bp, büyüklük $%.0f, net %s%s"
                    % (
                        sv,
                        lv,
                        cfg["wide_horizon"],
                        usd(funding),
                        usd(day1),
                        usd(day3),
                        usd(q["size"] * three),
                        usd(q["size"] * week),
                        sum(x > 0 for x in days),
                        stats["corr"],
                        stats["history_days"],
                        stats["corr30"],
                        stats["sd"] * 10000,
                        q["size"],
                        usd(net),
                        "".join("; " + n for n in context["notes"]),
                    )
                ),
            )
            if stats["corr"] < cfg["wide_min_corr"]:
                out["reasons"].append(
                    "7g getiri korelasyonu sınır altında · %.3f < %.2f"
                    % (stats["corr"], cfg["wide_min_corr"])
                )
            if not 0.8 <= stats["beta"] <= 1.25:
                out["reasons"].append(
                    "Hedge beta uyumsuz · %.2f (0,80–1,25 dışında)" % stats["beta"]
                )
            if abs(stats["z"]) > cfg["wide_max_z"]:
                out["reasons"].append(
                    "Spread z-score sınırı aşıldı · |z| %.1f > %.1f"
                    % (abs(stats["z"]), cfg["wide_max_z"])
                )
            if stats["sd"] > cfg["wide_max_spread_sd_bps"] / 10000:
                out["reasons"].append(
                    "Fiyat farkı istikrarlı değil (7g sd sınır üstünde) · %.1f bp > %.0f bp"
                    % (stats["sd"] * 10000, cfg["wide_max_spread_sd_bps"])
                )
            if day1 <= 0:
                out["reasons"].append(
                    "Gerçekleşmiş funding ile kalıcı avantaj yok · güncel ve son 24 saatin "
                    "kötüsüyle %s" % usd(day1)
                )
            if three <= 0:
                out["reasons"].append(
                    "3 günde gerçekleşen funding bu yönde avantaj göstermiyor · %s"
                    % usd(q["size"] * three)
                )
            if week <= 0:
                out["reasons"].append(
                    "7 günde gerçekleşen funding bu yönde avantaj göstermiyor · %s"
                    % usd(q["size"] * week)
                )
            elif week - max(days) <= 0:
                out["reasons"].append(
                    "Avantaj tek bir güne dayanıyor · 7g %s, en iyi gün hariç %s"
                    % (usd(q["size"] * week), usd(q["size"] * (week - max(days))))
                )
            if net < cfg["wide_min_net"]:
                out["reasons"].append(
                    "Funding geliri ücret + kayma + spread riski payını karşılamıyor · "
                    "net %s < $%.2f" % (usd(net), cfg["wide_min_net"])
                )
            worst = self.worst_loss(q)
            if worst > q["budget"] + 1e-6:
                out["reasons"].append(
                    "İşlem kayıp bütçesi aşılıyor · en kötü $%.2f > $%.2f"
                    % (worst, q["budget"])
                )
        except Exception as ex:
            out["reasons"].append(str(ex))
        out["reasons"] = list(dict.fromkeys(out["reasons"]))
        out["eligible"] = not out["reasons"]
        return out

    def assess_convergence(self, c):
        """Long cheap, short dear when the gap closed regularly over 7 and 30 days."""
        cfg = self.cfg
        a, lv, sv = c["asset"], c["long"], c["short"]
        out = dict(c, reasons=list(self.blocked(c)), eligible=False, net=None)
        try:
            at = now()
            left, right = self.price_history(lv, a), self.price_history(sv, a)
            stats = pair_stats(left, right, at, 1, cfg["wide_min_history_days"])
            hist = convergence_stats(left, right, at, c["spread"], cfg)
            out["stats"] = stats
            out["convergence_history"] = hist
            if hist["cross7"] < cfg["wide_conv_min_cross_7"] or hist["two_sided7"] < 4:
                out["reasons"].append(
                    "7 günde istikrarlı kesişim yok (%d kesişim, %d çift yönlü gün)"
                    % (hist["cross7"], hist["two_sided7"])
                )
            # The 30-day thresholds scale down when only a shorter window exists.
            days = hist["history_days"]
            if hist["cross30"] < math.ceil(
                cfg["wide_conv_min_cross_30"] * days / 30
            ) or hist["two_sided30"] < math.ceil(15 * days / 30):
                out["reasons"].append(
                    "%d günde istikrarlı kesişim yok (%d kesişim, %d çift yönlü gün)"
                    % (days, hist["cross30"], hist["two_sided30"])
                )
            # A constant basis is fine (it is the target); a drifting one is not.
            if abs(hist["baseline7"] - hist["baseline"]) > max(
                0.0002, hist["sd30"] * 0.5
            ):
                out["reasons"].append(
                    "Normal fiyat farkı kayıyor: 7g ve %dg medyanı uyuşmuyor" % days
                )
            if hist["episodes"] < cfg["wide_conv_min_episodes"]:
                raise ValueError(
                    "Bu genişlikte geçmiş makas kapanışı yetersiz (%d)"
                    % hist["episodes"]
                )
            expected = max(1, math.ceil(hist["avg_close_hours"]))
            max_hold = min(
                cfg["wide_conv_max_hold_hours"],
                max(2 * expected, math.ceil(hist["max_close_hours"])),
            )
            # Stress over the usual closing time, from 7d return residuals.
            stress = pair_stats(
                left, right, at, expected, cfg["wide_min_history_days"]
            )["stress_fraction"]
            q = self.priced(
                c,
                cfg["wide_conv_leverage"],
                stress,
                cfg["wide_size"],
                stats["vol"],
                max_hold,
            )
            raw = self.conservative_raw(c, at, required=False)
            # The gap may take until the maximum hold to close, and the funding is paid
            # all that time: a cost is charged over the longest hold the exits allow.
            window = at + max_hold * 3600
            rl = dict(interval=c["interval_long"], next=c["next_long"])
            rs = dict(interval=c["interval_short"], next=c["next_short"])
            funding = q["size"] * (
                raw["short"] * payments_within(rs, at, window)
                - raw["long"] * payments_within(rl, at, window)
            )
            # Funding is not the thesis: a cost is charged, income is not credited.
            funding_cost = min(0.0, funding)
            # Target: the pair's normal gap, the less favourable of the 7d and long-window
            # medians. It may be below zero when the short venue is usually the cheaper one.
            target = max(hist["baseline7"], hist["baseline"])
            # Expected capture: the gap narrowing back to its normal level, not beyond.
            capture = (
                q["qty"] * (q["entry_short"] - q["entry_long"]) - q["size"] * target
            )
            # The exit crosses half of today's round-trip book spread.
            exit_drag = q["execution_drag"] / 2
            # Risk reserve: one 7d standard deviation of the gap widening further.
            risk_reserve = q["size"] * hist["sd7"]
            net = (
                capture
                - exit_drag
                - q["entry_fee"]
                - q["exit_fee"]
                + funding_cost
                - risk_reserve
            )
            gap = math.log(q["entry_short"] / q["entry_long"])
            out.update(
                q,
                entry_capture=capture,
                exit_drag_proxy=exit_drag,
                conservative_funding=funding,
                funding_cost_reserve=funding_cost,
                conservative_raw=raw,
                risk_reserve=risk_reserve,
                expected_hours=expected,
                max_hold_hours=max_hold,
                entry_gap=gap,
                target_gap=target,
                net=net,
                entry_reason=(
                    "Yakınsama: long %s, short %s; boşluk %.1f bp (normal %.1f, sd %.1f), "
                    "kesişim 7g %d / %dg %d, ortalama kapanma %d saat, net $%.2f"
                    % (
                        lv,
                        sv,
                        gap * 10000,
                        target * 10000,
                        hist["sd7"] * 10000,
                        hist["cross7"],
                        hist["history_days"],
                        hist["cross30"],
                        expected,
                        net,
                    )
                ),
            )
            if gap < max(target + 0.0005, hist["baseline7"] + hist["sd7"]):
                out["reasons"].append(
                    "Fiyat boşluğu geçmiş dağılıma göre yeterli değil"
                )
            if min(stats["corr"], stats["corr30"]) < cfg["wide_min_corr"]:
                out["reasons"].append("7g / 30g getiri korelasyonu sınır altında")
            if net < cfg["wide_min_net"]:
                out["reasons"].append(
                    "Yakınsama kazancı 4 emir ücreti, kayma, funding gideri ve risk payı sonrası yetersiz"
                )
            if (
                q["entry_fee"] + q["exit_fee"] + q["execution_drag"] + q["stress_loss"]
                > q["budget"] + 1e-6
            ):
                out["reasons"].append("İşlem kayıp bütçesi aşılıyor")
        except Exception as ex:
            out["reasons"].append(str(ex))
        out["reasons"] = list(dict.fromkeys(out["reasons"]))
        out["eligible"] = not out["reasons"]
        return out

    def entry_funding_check(self, c):
        """Re-read funding right before the fills; None when the entry still holds."""
        try:
            fresh = {
                (r["venue"], r["asset"]): r
                for r in self.source.market_rows()
                if r.get("kind", "perp") == "perp"
            }
        except Exception as ex:
            return "Girişte funding yenilenemedi: " + str(ex)[:120]
        at = now()
        l, s = fresh.get((c["long"], c["asset"])), fresh.get((c["short"], c["asset"]))
        if not l or not s or l.get("raw_rate") is None or s.get("raw_rate") is None:
            return "Girişte güncel funding oranı yok"
        # A leg known only from the last bulk scan is re-read on its own.
        refresh = getattr(self.source, "current_funding", None)
        legs = []
        for r in (l, s):
            if refresh and at - r.get("funding_received", r["received"]) > (
                ENTRY_FUNDING_MAX_AGE
            ):
                try:
                    r = dict(r, **refresh(r["venue"], c["asset"]))
                except Exception:
                    pass
            legs.append(r)
        l, s = legs
        if any(
            at - r.get("funding_received", r["received"]) > ENTRY_FUNDING_MAX_AGE
            for r in (l, s)
        ):
            return "Girişte funding verisi eski"
        hours = (
            c["expected_hours"]
            if c.get("strategy") == "convergence"
            else self.cfg["wide_horizon"]
        )
        end = at + hours * 3600
        cash = c["size"] * (
            s["raw_rate"] * payments_within(s, at, end)
            - l["raw_rate"] * payments_within(l, at, end)
        )
        if c.get("strategy") == "convergence":
            net = c["net"] - c["funding_cost_reserve"] + min(0.0, cash)
        else:
            if cash <= 0:
                return "Girişte funding yenilendi: avantaj kalmadı"
            net = (
                c["net"]
                - c["conservative_funding"]
                + min(c["conservative_funding"], cash)
            )
        c["entry_funding_refresh"] = dict(cash=cash, net=net, at=at)
        if net < self.cfg["wide_min_net"]:
            return "Girişte funding yenilendi: net hedefin altına düştü"
        return None

    def book(self, v, a):
        started = time.monotonic()
        b = self.source.book(v, a)
        self.rtt[v].append(time.monotonic() - started)
        return b

    def latency(self, v):
        """Delay before a leg fills: the configured floor or the venue's measured p90
        round trip, whichever is longer. A real order travels the same network path."""
        seen = sorted(self.rtt[v])
        p90 = seen[int(0.9 * (len(seen) - 1))] if seen else 0.0
        return max(self.latency_ms / 1000, p90)

    def quote(self, v, a, qty, buy):
        b = self.book(v, a)
        return walk(b["asks" if buy else "bids"], qty, buy, self.extra_slippage), b

    def fill(self, v, a, qty, buy, limit, lots):
        """An entry order: immediate-or-cancel at `limit` on the venue's book now. A
        partial fill keeps what both legs' order sizes allow (the rest is cancelled);
        returns the quantity, its average price and the book."""
        b = self.book(v, a)
        side = b["asks" if buy else "bids"]
        got, px = ioc(side, qty, buy, limit, self.extra_slippage)
        if 0 < got < qty * (1 - 1e-9):
            got = lot_floor(got, px, lots)
            px = ioc(side, got, buy, limit, self.extra_slippage)[1] if got else None
        return got, px, b

    def unwind(self, p, leg, qty):
        """Close at once the part of a filled leg the other leg could not match: a real
        account would otherwise hold that much open directional exposure."""
        self.sleep(self.latency(leg["venue"]))
        px, b = self.quote(leg["venue"], p["asset"], qty, leg["side"] == "short")
        pnl = qty * (px - leg["entry"]) * (1 if leg["side"] == "long" else -1)
        fee = qty * px * leg["fee_rate"]
        self.credit(leg["venue"], pnl - fee)
        p["price_pnl"] += pnl
        p["fees"] += fee
        p["slippage"] = p.get("slippage", 0.0) + qty * abs(px - mid_of(b))
        leg["margin"] *= (leg["qty"] - qty) / leg["qty"]
        leg["qty"] -= qty
        p.setdefault("unwound", []).append(
            dict(venue=leg["venue"], qty=qty, price=px, pnl=pnl, fee=fee, at=now())
        )
        self.persist()
        self.events(
            "UNWIND",
            "Eşleşmeyen bacak kısmı hemen kapatıldı",
            position=p["id"],
            venue=leg["venue"],
            qty=qty,
            pnl=pnl,
            fee=fee,
        )

    def open(self, c):
        """Fill both legs; returns why nothing was entered when no leg filled, else None
        (a filled leg is a trade, even when it has to be recovered)."""
        p = dict(
            id="w" + uuid.uuid4().hex,
            asset=c["asset"],
            opened=now(),
            legs=[],
            funding=0.0,
            estimated_funding=0.0,
            fees=0.0,
            unrealized=0.0,
            price_pnl=0.0,
            status="OPENING",
            decision={k: v for k, v in c.items() if k != "stats"},
            close_reason=None,
            strategy=c.get("strategy", "funding"),
            entry_reason=c.get("entry_reason"),
            peak_net=None,
        )
        self.positions.append(p)
        self.persist()
        lots = [c.get("lot_long"), c.get("lot_short")]
        try:
            # Legs fill one after the other, each after its venue's latency, as
            # immediate-or-cancel orders at the planned price plus the tolerance. A
            # partial first leg sizes the second; a partial second leg leaves part of
            # the first unmatched, which is closed at once.
            qty = c["qty"]
            for venue, side in ((c["long"], "long"), (c["short"], "short")):
                buy = side == "long"
                self.sleep(self.latency(venue))
                planned = c["entry_long"] if buy else c["entry_short"]
                limit = planned * (1 + FILL_TOLERANCE if buy else 1 - FILL_TOLERANCE)
                got, px, b = self.fill(venue, c["asset"], qty, buy, limit, lots)
                if not got:
                    raise ValueError(
                        "Dolum olmadı: defter %0,1 fiyat sınırı içinde emir boyutunu "
                        "karşılamadı"
                    )
                if got < qty * (1 - 1e-9):
                    self.events(
                        "FILL_PARTIAL",
                        "Kısmi dolum",
                        position=p["id"],
                        venue=venue,
                        asked=qty,
                        filled=got,
                    )
                fee_rate = c["fee_long"] if buy else c["fee_short"]
                fee = got * px * fee_rate
                slip = got * abs(px - mid_of(b))
                p["slippage"] = p.get("slippage", 0.0) + slip
                margin = got * px / c["leverage"]
                if margin + fee > self.headroom(venue):
                    raise ValueError("Dolum öncesi marjin rezervi yetersiz")
                interval = c["interval_long"] if buy else c["interval_short"]
                nxt = c["next_long"] if buy else c["next_short"]
                entered = now()
                leg = dict(
                    venue=venue,
                    side=side,
                    qty=got,
                    entry=px,
                    entered=entered,
                    fee_rate=fee_rate,
                    entry_fee=fee,
                    margin=margin,
                    interval=interval,
                    first_payment=next_payment(
                        dict(interval=interval, next=nxt), entered
                    ),
                    closed=False,
                    price_pnl=0.0,
                    entry_slippage=slip,
                    audit_id=b.get("audit_id"),
                )
                p["legs"].append(leg)
                p["fees"] += fee
                self.credit(venue, -fee)
                self.persist()
                self.events("FILL", "Sanal bacak doldu", position=p["id"], leg=leg)
                if len(p["legs"]) == 2 and got < qty * (1 - 1e-9):
                    self.unwind(p, p["legs"][0], qty - got)
                qty = got
            ratio = qty / c["qty"]
            if ratio < 1 - 1e-9:
                d = p["decision"]
                d["planned_qty"] = c["qty"]
                for k in SIZED_FIELDS:
                    if isinstance(d.get(k), (int, float)):
                        d[k] *= ratio
            p["fill_ratio"] = ratio
            p["status"] = "OPEN"
            self.persist()
            self.events(
                "OPEN",
                "En iyi çoklu borsa fırsatı paper olarak açıldı",
                position=p["id"],
                strategy=p["strategy"],
                asset=c["asset"],
                long=c["long"],
                short=c["short"],
                net=c["net"],
            )
        except Exception as ex:
            if not p["legs"]:
                # Nothing was filled: an entry that did not happen is not a trade.
                why = "Giriş yapılmadı: " + str(ex)
                self.positions.remove(p)
                self.reject(c, [why], now())
                self.persist()
                self.events("ENTRY_SKIP", why, asset=c["asset"], strategy=p["strategy"])
                return why
            p["status"] = "RECOVERY"
            p["close_reason"] = "Bacak kurtarma: " + str(ex)
            self.persist()
            self.events("RECOVERY", p["close_reason"], position=p["id"])
            self.close(p, p["close_reason"])

    # --- monitoring ------------------------------------------------------------
    def settle(self, p):
        pending = []
        for leg in p["legs"]:
            v, a = leg["venue"], p["asset"]
            end = min(now(), leg.get("exited", now()))
            try:
                rows = self.funding_rows(v, a, leg["entered"])
            except Exception as ex:
                pending.append(v + ": funding geçmişi alınamadı (" + str(ex)[:80] + ")")
                continue
            seen = set()
            for r in rows:
                if not leg["entered"] < r["ts"] <= end:
                    continue
                t = int(round(r["ts"] / 60)) * 60
                seen.add(t)
                key = p["id"] + ":" + v + ":" + str(t)
                with self.store.lock:
                    exists = self.store.db.execute(
                        "select body from payments where key=?", (key,)
                    ).fetchone()
                late = bool(exists) and json.loads(exists[0]).get("unverified", False)
                if exists and not late:
                    continue
                estimated = False
                if r.get("value") is not None:
                    unit, price = r["value"], None
                else:
                    observed = self.store.before(v, a, r["ts"])
                    if observed:
                        price = observed[1]
                    elif now() - r["ts"] >= 3600:
                        # Reproducible estimate: the recorded entry fill, flagged in the record.
                        price, estimated = leg["entry"], True
                    else:
                        pending.append(key)
                        continue
                    unit = price * r["rate"]
                cash = leg["qty"] * unit * (-1 if leg["side"] == "long" else 1)
                body = dict(
                    position=p["id"],
                    venue=v,
                    asset=a,
                    rate=r["rate"],
                    cash=cash,
                    price=price,
                    valuation_estimated=estimated,
                    audit_id=r.get("audit_id"),
                )
                with self.store.lock:
                    self.credit(v, cash)
                    p["funding"] += cash
                    if estimated:
                        p["estimated_funding"] += cash
                    if late:
                        p["funding_unverified"] = [
                            x
                            for x in p.get("funding_unverified", [])
                            if (x["venue"], x["payment_time"]) != (v, t)
                        ]
                    self.store.db.execute(
                        "insert or replace into payments values(?,?,?)",
                        (key, r["ts"], json.dumps(body)),
                    )
                    self.persist()
                self.events(
                    "FUNDING_LATE" if late else "FUNDING",
                    "Funding ödemesi uzlaştırıldı",
                    position=p["id"],
                    venue=v,
                    payment_time=r["ts"],
                    cash=cash,
                )
            # Venue schedule known: a payment never published after the wait is
            # recorded as unverified zero, never invented.
            first, iv = leg.get("first_payment"), leg.get("interval")
            if first and iv:
                t = first
                while t <= end:
                    tm = int(round(t / 60)) * 60
                    if tm not in seen and now() - t >= FUNDING_UNVERIFIED_AFTER:
                        key = p["id"] + ":" + v + ":" + str(tm)
                        with self.store.lock:
                            exists = self.store.db.execute(
                                "select 1 from payments where key=?", (key,)
                            ).fetchone()
                            if not exists:
                                p.setdefault("funding_unverified", []).append(
                                    dict(
                                        payment_time=tm,
                                        venue=v,
                                        recorded=now(),
                                        risk=self.unverified_risk(p, leg, rows, tm),
                                    )
                                )
                                self.store.db.execute(
                                    "insert into payments values(?,?,?)",
                                    (
                                        key,
                                        tm,
                                        json.dumps(
                                            dict(
                                                position=p["id"],
                                                venue=v,
                                                asset=a,
                                                rate=None,
                                                cash=0.0,
                                                unverified=True,
                                            )
                                        ),
                                    ),
                                )
                                self.persist()
                        if not exists:
                            self.events(
                                "FUNDING_UNVERIFIED",
                                "Funding oranı yayımlanmadı; doğrulanmamış 0 kaydedildi",
                                position=p["id"],
                                venue=v,
                                payment_time=tm,
                            )
                    elif tm not in seen:
                        pending.append(v + ": " + str(tm))
                    t += iv
        p["pending_funding"] = pending

    def unverified_risk(self, p, leg, rows, t):
        """What an unpublished payment would have cost this leg at its last known rate
        (the latest payment it published, else the rate the entry was decided on); zero
        when it would have been income. The paper books the payment as 0, so on a paying
        leg its net looks better than it was; this is how much better at most."""
        known = [r for r in rows if r["ts"] < t and r.get("rate") is not None]
        if known:
            rate = max(known, key=lambda r: r["ts"])["rate"]
        else:
            d = p.get("decision") or {}
            rate = d.get("raw_long" if leg["side"] == "long" else "raw_short")
            if rate is None:
                rate = (self.rows.get((leg["venue"], p["asset"])) or {}).get("raw_rate")
        if rate is None:
            return 0.0
        seen = self.store.before(leg["venue"], p["asset"], t)
        price = seen[1] if seen else leg["entry"]
        cash = leg["qty"] * price * rate * (-1 if leg["side"] == "long" else 1)
        return min(0.0, cash)

    def mark_position(self, p, books):
        unrealized = exit_fee = slippage = 0.0
        for leg in p["legs"]:
            if leg["closed"]:
                continue
            b = books[leg["venue"]]
            buy = leg["side"] == "short"
            px = walk(
                b["asks" if buy else "bids"], leg["qty"], buy, self.extra_slippage
            )
            mid = (max(x for x, q in b["bids"]) + min(x for x, q in b["asks"])) / 2
            slippage += leg["qty"] * abs(px - mid)
            leg["price_pnl"] = (
                leg["qty"] * (px - leg["entry"]) * (1 if leg["side"] == "long" else -1)
            )
            leg["exit_estimate"] = px
            unrealized += leg["price_pnl"]
            exit_fee += leg["qty"] * px * leg["fee_rate"]
        p["unrealized"] = unrealized - exit_fee
        p["price_pnl_open"] = unrealized
        # What closing now costs: exit fees plus crossing both books from mid.
        p["exit_cost"] = exit_fee + slippage
        p["net_if_closed"] = p["price_pnl"] + p["funding"] - p["fees"] + p["unrealized"]
        p["last_mark"] = now()

    def close(self, p, reason):
        p["status"] = "CLOSING"
        p["close_reason"] = reason
        self.persist()
        for leg in p["legs"]:
            if leg["closed"]:
                continue
            try:
                self.sleep(self.latency(leg["venue"]))
                px, b = self.quote(
                    leg["venue"], p["asset"], leg["qty"], leg["side"] == "short"
                )
                pnl = (
                    leg["qty"]
                    * (px - leg["entry"])
                    * (1 if leg["side"] == "long" else -1)
                )
                fee = leg["qty"] * px * leg["fee_rate"]
                self.credit(leg["venue"], pnl - fee)
                p["price_pnl"] += pnl
                p["fees"] += fee
                if "slippage" in p:
                    p["slippage"] += leg["qty"] * abs(px - mid_of(b))
                leg.update(
                    closed=True,
                    exit=px,
                    exited=now(),
                    price_pnl=0.0,
                    exit_audit_id=b.get("audit_id"),
                )
                self.persist()
            except Exception as ex:
                p["status"] = "RECOVERY"
                self.events(
                    "RECOVERY",
                    "Kapatılamayan sanal bacak; sonraki taramada tekrar",
                    position=p["id"],
                    error=str(ex),
                )
                self.persist()
                return
        p["unrealized"] = 0.0
        self.settle(p)
        p["status"] = "CLOSED"
        p["closed_at"] = now()
        p["net"] = p["price_pnl"] + p["funding"] - p["fees"]
        self.positions.remove(p)
        self.closed.append(p)
        self.manual_close.discard(p["id"])
        self.persist()
        self.record_close(p)
        self.events("CLOSE", reason, position=p["id"], net=p["net"])

    def exit_reason(self, p, books):
        cfg, d = self.cfg, p["decision"]
        if p["id"] in self.manual_close:
            return "Kullanıcı paper kapatma talebi"
        if p["status"] in ("RECOVERY", "CLOSING", "OPENING") or any(
            l["closed"] for l in p["legs"]
        ):
            return p.get("close_reason") or "Bacak kurtarma"
        if p["net_if_closed"] <= -cfg["wide_risk"]:
            return "Pozisyon kayıp limiti"
        if self.daily_pnl() <= -cfg["wide_daily_loss"]:
            return "Günlük kayıp limiti"
        if p["price_pnl_open"] + d["execution_drag"] < -d["stress_loss"]:
            return "Spread stres bütçesi aşıldı"
        # Each venue holds its own collateral: one leg's loss is not covered by the other.
        for leg in p["legs"]:
            if (
                not leg["closed"]
                and -leg.get("price_pnl", 0) >= LEG_SAFETY * leg["margin"]
            ):
                return "Bacak marjin riski: " + leg["venue"]
        # A venue's account is liquidated on its own equity, whatever the other venue holds.
        if self.per_venue():
            for leg in p["legs"]:
                b = self.balance(leg["venue"])
                if not leg["closed"] and b["equity"] <= LEG_SAFETY * b["margin"]:
                    return "Borsa teminatı riski: " + leg["venue"]
        held = now() - p["opened"]
        if p.get("strategy") == "convergence":
            mid = {
                v: (max(x for x, q in b["bids"]) + min(x for x, q in b["asks"])) / 2
                for v, b in books.items()
            }
            gap = math.log(mid[d["short"]] / mid[d["long"]])
            p["live_gap"] = gap
            # Earlier positions targeted max(mean, 0); the baseline target may be negative.
            zone = d.get("target_gap", 0.0) + cfg["wide_conv_exit_bps"] / 10000
            # No need to wait for an exact cross: in the mean/cross zone the
            # after-close net decides.
            if gap <= zone and p["net_if_closed"] >= cfg["wide_min_net"]:
                return "Fark ortalama/kesişim bölgesine geldi; kapanış sonrası net hedef sağlandı"
            if gap <= d.get("target_gap", 0.0) and p["net_if_closed"] > 0:
                return "Fiyat farkı normal seviyesine döndü"
            if p["net_if_closed"] >= d["net"] > 0:
                return "Beklenen yakınsama kazancı alındı"
            if held > d["max_hold_hours"] * 3600:
                return "Yakınsama azami bekleme süresi"
            return None
        if held > cfg["wide_max_hold_hours"] * 3600:
            return "Azami tutma süresi"
        if p["net_if_closed"] >= d["net"] > 0:
            return "Kâr hedefi alındı"
        peak = max(p.get("peak_net") or p["net_if_closed"], p["net_if_closed"])
        p["peak_net"] = peak
        # Profit lock: once a real gain exists, a pullback of the trail fraction closes it
        # while still positive. Entry fees mean a fresh trade starts below zero.
        if peak >= cfg["wide_min_net"] and 0 < p["net_if_closed"] <= peak * (
            1 - cfg["wide_lock_trail"]
        ):
            return "Kâr kilitlendi: kazanç geri verilmeye başladı"
        try:
            stats = pair_stats(
                self.price_history(d["long"], p["asset"]),
                self.price_history(d["short"], p["asset"]),
                now(),
                cfg["wide_horizon"],
                cfg["wide_min_history_days"],
            )
        except Exception:
            stats = None
        if stats:
            p["live_stats"] = {k: stats[k] for k in ("corr", "corr30", "z", "sd")}
            # The hedge is judged on the last 7 days, as at entry; the long window
            # is context and never closes a position.
            if (
                stats["corr"] < cfg["wide_min_corr"]
                or abs(stats["z"]) > cfg["wide_max_z"] + 1
            ):
                return "Fiyat uyumu bozuldu"
        l, s = self.rows.get((d["long"], p["asset"])), self.rows.get(
            (d["short"], p["asset"])
        )
        if l and s and l.get("raw_rate") is not None and s.get("raw_rate") is not None:
            end = now() + cfg["wide_horizon"] * 3600
            ahead = s["raw_rate"] * payments_within(s, now(), end) - l[
                "raw_rate"
            ] * payments_within(l, now(), end)
            p["remaining_funding_fraction"] = ahead
            # Leaving costs the exit fees and spread now. A reversal that costs less than
            # that over the horizon is cheaper to hold through; the loss limits above
            # still close the position whenever they are hit.
            flipped = ahead < 0 and -ahead * d["size"] > p.get("exit_cost", 0.0)
            weak = flipped or (
                p["net_if_closed"] >= cfg["wide_min_net"]
                and ahead * d["size"] < cfg["wide_min_net"]
            )
            # A single quote does not trigger an exit: the weakness must persist.
            if not weak:
                p["weak_since"] = None
            else:
                p["weak_since"] = p.get("weak_since") or now()
                if now() - p["weak_since"] >= cfg["wide_weak_minutes"] * 60:
                    return (
                        "Funding avantajı tersine döndü"
                        if flipped
                        else "Funding zayıfladı; kâr alındı"
                    )
        return None

    def report(self):
        """Paper results per strategy; unverified and estimated funding kept apart."""
        out = {}
        # Every trade of the period, not only the last 500 kept in full.
        trades = self.period_trades()
        for key in ("funding", "convergence"):
            done = [p for p in trades if p["strategy"] == key]
            settled = [p for p in done if not p["pending"]]
            out[key] = dict(
                open=sum(p.get("strategy", "funding") == key for p in self.positions),
                trades=len(done),
                settled=len(settled),
                wins=sum(p["net"] > 0 for p in settled),
                net=sum(p["net"] for p in settled),
                price_pnl=sum(p["price_pnl"] for p in settled),
                funding=sum(p["funding"] for p in settled),
                estimated_funding=sum(p["estimated_funding"] for p in settled),
                fees=sum(p["fees"] for p in settled),
                unverified_payments=sum(p["unverified"] for p in done),
                pending=len(done) - len(settled),
            )
        return out

    def cycle(self, entries=True):
        self.error = None
        try:
            try:
                rows = self.with_fees(self.source.market_rows())
            except Exception as ex:
                # Open positions are still marked and closed from the books.
                rows = None
                self.error = "Piyasa listesi alınamadı: " + str(ex)
            if rows is not None:
                self.rows = {
                    (r["venue"], r["asset"]): r
                    for r in rows
                    if r.get("kind", "perp") == "perp"
                }
            open_keys = {
                (l["venue"], p["asset"]) for p in self.positions for l in p["legs"]
            }
            for key in open_keys:
                r = self.rows.get(key)
                if r:
                    self.store.mark(key[0], key[1], r["received"], r["mark"])
            for p in list(self.positions):
                try:
                    self.settle(p)
                    books = {
                        l["venue"]: self.book(l["venue"], p["asset"])
                        for l in p["legs"]
                        if not l["closed"]
                    }
                    self.mark_position(p, books)
                    reason = self.exit_reason(p, books)
                    if reason:
                        self.close(p, reason)
                    else:
                        p["status"] = "OPEN"
                except Exception as ex:
                    if p["status"] == "OPEN":
                        p["status"] = "DATA_WAIT"
                    self.events(
                        "DATA_WAIT",
                        "Açık pozisyon riski güncellenemedi",
                        position=p["id"],
                        error=str(ex),
                    )
            for p in self.closed[-50:]:
                # Late-published rates replace unverified zero hours for a week after close.
                if p.get("pending_funding") or (
                    p.get("funding_unverified")
                    and now() - p.get("closed_at", now()) < LATE_FUNDING_WINDOW
                ):
                    self.settle(p)
                    p["net"] = p["price_pnl"] + p["funding"] - p["fees"]
                    self.record_close(p)
            at = now()
            # Per-venue mode trades only where an account (and its collateral) exists.
            ranked_rows = (
                [r for r in rows if r["venue"] in self.wallet["venues"]]
                if rows is not None and self.per_venue()
                else rows
            )
            self.candidates = (
                rank(ranked_rows, self.cfg, at)
                if rows is not None and self.cfg["wide_funding_enabled"]
                else []
            )
            self.conv_candidates = (
                rank_spreads(ranked_rows, at)
                if rows is not None and self.cfg["wide_conv_enabled"]
                else []
            )
            # RFQ pairs leave before selection so they cannot use up deep-check slots.
            self.candidates, rfq_funding = split_rfq(self.candidates)
            self.conv_candidates, rfq_conv = split_rfq(self.conv_candidates)
            self.rfq_review = rfq_funding + rfq_conv
            self.assessed = []
            if (
                entries
                and rows is not None
                and self.cfg["wide_enabled"]
                and len(self.positions) < self.cfg["wide_max_positions"]
                and not any(p["status"] != "OPEN" for p in self.positions)
            ):
                self.rejected = {
                    k: x for k, x in self.rejected.items() if x["until"] > at
                }
                self.review["rounds"] += 1
                self.round = {}
                top = self.cfg["wide_deep_top"]
                for ranked in (self.candidates, self.conv_candidates):
                    picked, counted = [], 0
                    for c in ranked:
                        # Pairs still waiting for history fail fast and do not use up a
                        # deep-check slot; the total per round stays bounded.
                        if counted >= top or len(picked) >= top * 4 or c["score"] <= 0:
                            break
                        if (
                            self.blocked(c)
                            or any(x["asset"] == c["asset"] for x in picked)
                            or self.pair_key(c) in self.rejected
                        ):
                            self.review["skipped"] += 1
                            continue
                        x = self.assess(c)
                        self.checked[self.pair_key(c)] = at
                        picked.append(x)
                        if not any(
                            reason_group(r) == "Geçmiş eksik" for r in x["reasons"]
                        ):
                            counted += 1
                    self.assessed += picked
                for x in self.assessed:
                    self.note_review(x, at)
                # Every qualifying pair is entered, best first, one per asset, while
                # position slots and margin headroom last.
                ready = sorted(
                    (x for x in self.assessed if x["eligible"]),
                    key=lambda x: -x["net"],
                )
                for i, best in enumerate(ready):
                    # An earlier entry this round may have left a leg in recovery or
                    # used up the day's loss budget: check again before every entry.
                    halt = self.entry_halt()
                    if halt:
                        self.events("ENTRY_HALT", "Yeni girişler durduruldu: " + halt)
                        for x in ready[i:]:
                            self.decide(
                                x, at, "skipped", ["Yeni girişler durdu: " + halt]
                            )
                        break
                    if self.blocked(best):
                        self.decide(best, at, "skipped", self.blocked(best))
                        continue
                    # Sized against the headroom before earlier entries this round.
                    if self.per_venue():
                        fees = (best["fee_long"], best["fee_short"])
                        if any(
                            (1 + FILL_TOLERANCE)
                            * best["size"]
                            * (1 / best["leverage"] + f)
                            > self.headroom(v)
                            for v, f in zip((best["long"], best["short"]), fees)
                        ):
                            self.decide(
                                best, at, "skipped", ["Borsa teminatı yetersiz"]
                            )
                            continue
                    else:
                        need = (1 + FILL_TOLERANCE) * (
                            2 * best["size"] / best["leverage"] + best["entry_fee"]
                        )
                        if need > self.headroom():
                            self.decide(best, at, "skipped", ["Teminat yetersiz"])
                            continue
                    if self.worst_loss(best) > self.risk_budget() + 1e-6:
                        self.decide(
                            best, at, "skipped", ["Günün kalan kayıp bütçesi yetmiyor"]
                        )
                        continue
                    stale = self.entry_funding_check(best)
                    if stale:
                        best["eligible"] = False
                        best["reasons"].append(stale)
                        self.review["eligible"] -= 1
                        self.reject(best, [stale], at)
                        self.decide(best, at, "rejected")
                        self.events(
                            "ENTRY_SKIP",
                            stale,
                            asset=best["asset"],
                            strategy=best["strategy"],
                        )
                    else:
                        missed = self.open(best)
                        if missed:
                            # Eligible, but the orders filled nothing: not a trade.
                            self.decide(best, at, "skipped", [missed])
                        else:
                            self.review["opened"] += 1
                            self.decide(best, at, "opened", [])
            if rows is not None:
                self.funnel = self.build_funnel(rows, ranked_rows, at)
            self.last_cycle = now()
            self.record_run(self.last_cycle, entries and self.cfg["wide_enabled"])
            self.persist()
        except Exception as ex:
            self.error = str(ex)
            self.events("ERROR", str(ex))

    def record_run(self, at, entries=True):
        """Downtime between cycles, time with new entries switched off, and an hourly
        equity sample, for the period report."""
        run = self.store.get("wide_run", {})
        last = run.get("last")
        if last is not None:
            if at - last > DOWNTIME_GAP:
                run.setdefault("gaps", []).append([last, at])
            elif not entries:
                run["paused"] = run.get("paused", 0.0) + at - last
        run["last"] = at
        self.store.put("wide_run", run)
        self.mark_epoch(at)
        samples = self.store.get("wide_equity", [])
        t = hour(at)
        if not samples or samples[-1][0] != t:
            equity = self.wallet["cash"] + sum(
                p.get("unrealized", 0) for p in self.positions
            )
            samples.append([t, equity])
            self.store.put("wide_equity", samples[-24 * 120 :])

    @staticmethod
    def ledger_entry(p):
        side = {leg.get("side"): leg.get("venue") for leg in p["legs"]}
        return dict(
            asset=p.get("asset"),
            long=side.get("long"),
            short=side.get("short"),
            opened=p.get("opened"),
            closed_at=p.get("closed_at"),
            size=(p.get("decision") or {}).get("size"),
            strategy=p.get("strategy", "funding"),
            net=p.get("net", 0.0),
            price_pnl=p.get("price_pnl", 0.0),
            funding=p.get("funding", 0.0),
            estimated_funding=p.get("estimated_funding", 0.0),
            fees=p.get("fees", 0.0),
            venues=[leg.get("venue") for leg in p["legs"]],
            entry_reason=p.get("entry_reason"),
            reason=p.get("close_reason"),
            pending=bool(p.get("pending_funding")),
            unverified=len(p.get("funding_unverified", [])),
            unverified_risk=sum(
                x.get("risk") or 0.0 for x in p.get("funding_unverified", [])
            ),
            fill_ratio=p.get("fill_ratio", 1.0),
            slippage=p.get("slippage"),
        )

    def record_close(self, p):
        """Only the last 500 closed trades are kept in full; this compact ledger keeps
        every trade of the period, so a long period never loses its early results."""
        if not p.get("legs") or not p.get("id"):
            return
        ledger = self.store.get("wide_ledger", {})
        entry = self.ledger_entry(p)
        if ledger.get(p["id"]) != entry:
            ledger[p["id"]] = entry
            self.store.put("wide_ledger", ledger)

    def period_trades(self):
        trades = dict(self.store.get("wide_ledger", {}))
        for i, p in enumerate(self.closed):
            if p.get("legs"):
                trades[p.get("id") or "closed-%d" % i] = self.ledger_entry(p)
        return [dict(x, id=k) for k, x in trades.items()]

    def trade_log(self):
        """Every closed trade of the period, latest first."""
        return sorted(self.period_trades(), key=lambda x: -(x.get("closed_at") or 0))

    def period_report(self):
        """Is this period's result trustworthy? Each check is shown with its threshold;
        none of them is a guarantee of future results."""
        at = now()
        start = self.store.get("wide_epoch_start", None) or at
        run = self.store.get("wide_run", {})
        gaps = run.get("gaps", [])
        if run.get("last") is not None and at - run["last"] > DOWNTIME_GAP:
            gaps = gaps + [[run["last"], at]]
        down = sum(min(b, at) - max(a, start) for a, b in gaps if b > start)
        span = max(at - start, 1)
        paused = run.get("paused", 0.0)
        changes = [t for t in run.get("changes", []) if t >= start]
        opened = [p for p in self.positions if p.get("legs")]
        open_net = sum(p.get("net_if_closed", 0) for p in opened)
        done = self.period_trades()
        settled = [p for p in done if not p["pending"]]
        nets = sorted((p["net"] for p in settled), reverse=True)
        total = sum(nets)
        gains = sum(x for x in nets if x > 0)
        losses = -sum(x for x in nets if x < 0)
        peak = drawdown = 0.0
        for _, equity in self.store.get("wide_equity", []):
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
        venues = {}
        for p in done:
            for v in p["venues"]:
                venues[v] = venues.get(v, 0) + 1
        collateral = sum(
            str(p["reason"] or "").startswith("Borsa teminatı riski") for p in done
        )
        unverified = sum(p["unverified"] for p in done)
        unverified_risk = sum(p.get("unverified_risk") or 0.0 for p in settled)
        pf = gains / losses if losses else (None if not gains else math.inf)
        top3 = sum(nets[:3]) / total if total > 0 else None
        days = span / 86400
        stress = [
            dict(label=label, net=stressed(settled, **kw)) for label, kw in STRESS
        ]
        t = PERIOD_CHECKS
        checks = [
            ("Süre", f"{days:.1f} / {t['days']} gün", days >= t["days"]),
            (
                "Kesintisiz izleme",
                f"%{down / span * 100:.1f} kesinti (sınır %{t['downtime_share'] * 100:.0f})",
                down / span <= t["downtime_share"],
            ),
            (
                "İşlem sayısı",
                f"{len(settled)} / {t['trades']}",
                len(settled) >= t["trades"],
            ),
            ("Ücretler sonrası net", f"{total:+.2f} $", total > 0),
            (
                "Stres altında net (hepsi birlikte)",
                f"{stress[-1]['net']:+.2f} $",
                stress[-1]["net"] > 0,
            ),
            (
                "Kazanç / kayıp oranı",
                "—" if pf is None else ("kayıp yok" if pf == math.inf else f"{pf:.2f}"),
                pf is not None and pf >= t["profit_factor"],
            ),
            (
                "En iyi 3 işlemin nete payı",
                "—" if top3 is None else f"%{top3 * 100:.0f}",
                top3 is not None and top3 < t["top3_share"],
            ),
            (
                "Yeni girişler açık kaldı",
                f"%{paused / span * 100:.1f} kapalı (sınır %{t['paused_share'] * 100:.0f})",
                paused / span <= t["paused_share"],
            ),
            (
                "Doğrulanmamış funding aleyhte sayılınca net",
                f"{unverified} ödeme · {total + unverified_risk:+.2f} $",
                total + unverified_risk > 0,
            ),
            ("Dönem içinde ayar değişikliği", str(len(changes)), not changes),
            ("Borsa teminatı riski kapanışı", str(collateral), collateral == 0),
        ]
        return dict(
            start=start,
            days=days,
            downtime_hours=down / 3600,
            gaps=len(gaps),
            paused_hours=paused / 3600,
            changes=len(changes),
            open=len(opened),
            open_net=open_net,
            trades=len(done),
            settled=len(settled),
            wins=sum(x > 0 for x in nets),
            net=total,
            profit_factor=None if pf in (None, math.inf) else pf,
            top3_share=top3,
            max_drawdown=drawdown,
            capital=self.cfg["wide_capital"],
            collateral_closes=collateral,
            unverified_payments=unverified,
            unverified_risk=unverified_risk,
            venues=dict(sorted(venues.items(), key=lambda kv: -kv[1])),
            checks=[dict(label=a, value=b, ok=bool(c)) for a, b, c in checks],
            ready=all(c for _, _, c in checks),
            stress=stress,
            stress_unmeasured=sum(p.get("slippage") is None for p in settled),
            scorecard=scorecard(settled),
        )

    @staticmethod
    def pair_key(c):
        return (c.get("strategy") or "funding", c["asset"], c["long"], c["short"])

    def note_review(self, x, at):
        """Count a deep check; a rejected pair is set aside so later rounds move down the list."""
        r = self.review
        r["examined"] += 1
        r["eligible"] += bool(x["eligible"])
        if not x["eligible"]:
            self.reject(x, x["reasons"] or ["Bilinmeyen"], at)
        self.decide(x, at, "eligible" if x["eligible"] else "rejected")

    def decide(self, x, at, outcome, reasons=None):
        """Keep the desk's latest word on a pair: eligible, rejected, skipped (eligible
        but not entered) or opened, with the reasons and the expected net."""
        if reasons is None:
            reasons = x.get("reasons") or []
        # A pair passes through several outcomes in one round (eligible, then opened,
        # skipped or rejected at entry); the round counts its last one only.
        self.round.setdefault(x.get("strategy") or "funding", {})[
            self.pair_key(x)
        ] = outcome
        d = dict(at=at, outcome=outcome, reasons=list(reasons)[:4], net=x.get("net"))
        context = x.get("context") or {}
        if context.get("notes"):
            d.update(notes=context["notes"][:3], confidence=context["confidence"])
        self.decisions[self.pair_key(x)] = d
        if len(self.decisions) > 5000:
            for k in sorted(self.decisions, key=lambda k: self.decisions[k]["at"])[
                :1000
            ]:
                del self.decisions[k]

    def verdict(self, asset, a, b):
        """What the desk did with one asset on two venues, per strategy: data for each
        leg, the candidate rank, the latest decision and when a rejected pair returns.
        """
        venues = {a, b}

        def same(x):
            return x.get("asset") == asset and {x.get("long"), x.get("short")} == venues

        at = now()
        legs = {}
        for v in (a, b):
            r = self.rows.get((v, asset))
            if self.per_venue() and v not in self.wallet["venues"]:
                legs[v] = "hesap listesinde değil"
            elif not r:
                legs[v] = "piyasa verisi yok"
            elif r.get("fee") is None:
                legs[v] = "taker ücreti bilinmiyor"
            elif not r.get("interval") or r.get("raw_rate") is None:
                legs[v] = "funding oranı/aralığı yok"
            elif at - r["received"] > ROW_MAX_AGE:
                legs[v] = "veri eski"
            elif r.get("rfq"):
                legs[v] = "RFQ piyasası"
            else:
                legs[v] = None
        out = dict(
            legs=legs,
            positions=[
                dict(
                    strategy=p.get("strategy", "funding"),
                    long=next(
                        (l["venue"] for l in p["legs"] if l["side"] == "long"), None
                    ),
                    short=next(
                        (l["venue"] for l in p["legs"] if l["side"] == "short"), None
                    ),
                    opened=p.get("opened"),
                    net_if_closed=p.get("net_if_closed"),
                    entry_reason=p.get("entry_reason"),
                )
                for p in self.positions
                if p["asset"] == asset
            ],
            trades=[x for x in self.trade_log() if same(x)][:5],
            strategies=[],
        )
        for key, ranked, flag in (
            ("funding", self.candidates, "wide_funding_enabled"),
            ("convergence", self.conv_candidates, "wide_conv_enabled"),
        ):
            c = next((x for x in ranked if same(x)), None)
            d = next(
                (
                    dict(v, long=k[2], short=k[3])
                    for k, v in self.decisions.items()
                    if k[0] == key and k[1] == asset and {k[2], k[3]} == venues
                ),
                None,
            )
            wait = next(
                (x for x in self.rejected.values() if x["strategy"] == key and same(x)),
                None,
            )
            out["strategies"].append(
                dict(
                    strategy=key,
                    enabled=bool(self.cfg[flag]),
                    rank=c["rank"] if c else None,
                    candidates=len(ranked),
                    score=c["score"] if c else None,
                    long=(c or d or {}).get("long"),
                    short=(c or d or {}).get("short"),
                    decision=d,
                    retry_at=wait["until"] if wait else None,
                )
            )
        return out

    def reject(self, x, reasons, at):
        groups = list(dict.fromkeys(reason_group(t) for t in reasons))
        for g in groups:
            self.review["reasons"][g] = self.review["reasons"].get(g, 0) + 1
        wait = (
            HISTORY_RETRY
            if groups[0] == "Geçmiş eksik"
            else max(reason_wait(g) for g in groups)
        )
        self.rejected[self.pair_key(x)] = dict(
            strategy=x.get("strategy") or "funding",
            asset=x["asset"],
            long=x["long"],
            short=x["short"],
            reason=reasons[0],
            group=groups[0],
            at=at,
            until=at + wait,
        )

    def build_funnel(self, rows, ranked_rows, at):
        """From the scan to an opened trade, with counts at every step: perp markets
        scanned, markets left out (by reason), venue pairs, ranked candidates and those
        positive after fees, pairs deep-checked in the last hour, how far down each list
        the deep check has reached, and what the latest round decided."""
        self.checked = {
            k: t for k, t in self.checked.items() if at - t < CHECKED_WINDOW
        }
        waiting = [x for x in self.rejected.values() if x["until"] > at]
        perps = [r for r in rows if r.get("kind", "perp") == "perp"]
        why = {}
        if len(ranked_rows) < len(rows):
            why["Hesap borsası değil"] = sum(
                r.get("kind", "perp") == "perp" for r in rows
            ) - sum(r.get("kind", "perp") == "perp" for r in ranked_rows)
        groups = usable_rows(ranked_rows, at, why)
        out = dict(
            at=at,
            markets=len(perps),
            usable=sum(len(g) for g in groups.values()),
            excluded=dict(sorted(why.items(), key=lambda kv: -kv[1])),
            assets=sum(len(g) > 1 for g in groups.values()),
            pairs=sum(len(g) * (len(g) - 1) // 2 for g in groups.values()),
            strategies={},
        )
        for key, ranked, flag in (
            ("funding", self.candidates, "wide_funding_enabled"),
            ("convergence", self.conv_candidates, "wide_conv_enabled"),
        ):
            positive = [c for c in ranked if c["score"] > 0]
            # How far down the list the deep check has reached: leading candidates that
            # were checked within the cooldown, are set aside, or are blocked.
            reached = 0
            for c in positive:
                k = self.pair_key(c)
                if (
                    self.rejected.get(k, {}).get("until", 0) > at
                    or at - self.checked.get(k, -math.inf) < REJECT_COOLDOWN
                    or self.blocked(c)
                ):
                    reached += 1
                    continue
                break
            out["strategies"][key] = dict(
                enabled=bool(self.cfg[flag]),
                candidates=len(ranked),
                positive=len(positive),
                reached=reached,
                checked=sum(k[0] == key for k in self.checked),
                waiting=sum(x["strategy"] == key for x in waiting),
                round=round_counts(self.round.get(key, {})),
            )
        return out

    def review_snapshot(self):
        waiting = {}
        for x in self.rejected.values():
            waiting[x["group"]] = waiting.get(x["group"], 0) + 1
        return dict(
            self.review,
            reasons=dict(sorted(self.review["reasons"].items(), key=lambda kv: -kv[1])),
            waiting=dict(sorted(waiting.items(), key=lambda kv: -kv[1])),
            rejected=sorted(self.rejected.values(), key=lambda x: -x["at"])[:40],
            cooldown=REJECT_COOLDOWN,
            history_retry=HISTORY_RETRY,
        )

    def history_needs(self, limit=40):
        """(venue, asset) legs of tradeable candidates, rejected-for-history first."""
        out = []
        for x in sorted(self.rejected.values(), key=lambda x: -x["at"]):
            if x["group"] == "Geçmiş eksik":
                out += [(x["long"], x["asset"]), (x["short"], x["asset"])]
        for ranked in (self.candidates, self.conv_candidates):
            for c in ranked[:limit]:
                if c["score"] > 0:
                    out += [(c["long"], c["asset"]), (c["short"], c["asset"])]
        return list(dict.fromkeys(out))

    def snapshot(self):
        venues = sorted({v for v, a in self.rows})
        return dict(
            config=self.cfg,
            balance=self.balance(),
            venue_balances=self.venue_balances(),
            account_venues=list(ACCOUNT_VENUES),
            venue_choices=venue_choices(),
            fees=self.fees,
            daily_pnl=self.daily_pnl(),
            positions=self.positions,
            closed=self.closed[-100:],
            report=self.report(),
            candidates=self.candidates[:40],
            candidate_count=len(self.candidates),
            conv_candidates=self.conv_candidates[:40],
            conv_candidate_count=len(self.conv_candidates),
            rfq_review=self.rfq_review[:40],
            rfq_review_count=len(self.rfq_review),
            assessed=[{k: v for k, v in c.items()} for c in self.assessed],
            review=self.review_snapshot(),
            funnel=self.funnel,
            epochs=self.epochs(),
            period=self.period_report(),
            trades=self.trade_log()[:200],
            epoch_start=self.store.get("wide_epoch_start", None),
            venues=venues,
            last_cycle=self.last_cycle,
            error=self.error,
            method=(
                "İki strateji, taranan tüm borsa çiftleri ve tüm varlıklar (seçili liste dışındakiler OI veya "
                "hacmi ≥ $1M bacaklarla); net = iki bacağın fiyat PnL'si + net funding − "
                "komisyon, kayma dolum fiyatlarında. Funding: bugünkü oran ve son 24 saatte ödenen "
                "funding (bacak başına kötüsü) avantaj göstermeli; son 3 ve 7 günde bu yönde gerçekten "
                "ödenen funding pozitif olmalı ve tek bir güne dayanmamalı; fiyatlar son 7 günde birlikte "
                "hareket etmeli (korelasyon, beta, z, fark sd). Beklenen funding 1g tahmini ile 3g "
                "ortalamasından düşük olanı; ücret, kayma ve fark riski payını karşılarsa açılır. 30 güne "
                "kadar uzun dönem yalnızca bağlamdır: korelasyon ya da funding kısa dönemle çelişirse "
                "büyüklük yarıya iner. Zayıflık kalıcıysa, 7g uyum bozulursa veya bacak marjini riske "
                "girerse çıkılır. "
                "Yakınsama: çiftin normal fiyat farkı (7g/30g medyanı; sıfır olması gerekmez) etrafında 7g ve "
                "30g düzenli kesişim ve dönüş süresi; ucuzda long, pahalıda short, eşit miktar, en fazla 5x; "
                "fark normal seviyesine dönünce kapanış sonrası nete bakılarak kapanır; normal fark "
                "kayıyorsa girilmez. "
                "Kaldıraç tavandır, her bacağın kendi marjinine göre küçülür. Gerçek emir ve API anahtarı yok."
            ),
        )
