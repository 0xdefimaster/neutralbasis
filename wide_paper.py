"""Multi-venue paper desk: two strategies over every scanned perp pair.

1. Funding: carry on pairs whose prices stay correlated over 7 and 30 days; profit is
   locked once a gain starts being given back, and the trade leaves when carry turns.
2. Convergence: long the cheaper venue, short the dearer, equal size, when the pair's
   prices crossed regularly over both 7 and 30 days; close when they cross again.

Public data only. No order endpoint, no credentials; every fill is simulated against
the venue's real public order book. Capital is one pooled paper wallet.
"""

import datetime as dt, itertools, json, math, statistics, time, uuid
from bot import now, hour, number, walk, corr, FILL_TOLERANCE, FUNDING_UNVERIFIED_AFTER

# Native venues whose taker fee is configured; the rest come from the CCXT scanner.
NATIVE = ("Lighter", "Extended")
WIDE_DEFAULTS = dict(
    wide_enabled=True,
    wide_capital=1000.0,
    wide_size=500.0,
    wide_leverage=3.0,
    wide_reserve=0.30,
    wide_max_positions=2,
    wide_horizon=24,
    wide_min_net=0.50,
    wide_risk=10.0,
    wide_daily_loss=30.0,
    wide_min_corr=0.90,
    wide_max_z=3.0,
    wide_deep_top=3,
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
FUNDING_TTL = 120
MIN_NOTIONAL = 10
LATE_FUNDING_WINDOW = 7 * 86400
# Funding quotes older than this at entry are refreshed or the entry is skipped.
ENTRY_FUNDING_MAX_AGE = 120


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


def usable_rows(rows, at):
    """One fresh perp market per venue and asset with a known taker fee and schedule."""
    groups = {}
    for r in rows:
        if (
            r.get("kind", "perp") == "perp"
            and r.get("rate") is not None
            and r.get("raw_rate") is not None
            and r.get("interval")
            and r.get("fee") is not None
            and (r.get("mark") or 0) > 0
            and r.get("price_type") != "last_trade"
            and at - r["received"] <= 180
        ):
            groups.setdefault(r["asset"], {}).setdefault(r["venue"], r)
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
    )


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
        raise ValueError("%d günlük eşlenmiş 1h mum eksik" % (hours // 24))
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
    """Regular, stable crossings over both 7 and 30 days, and the usual time to close.

    Gap is log(short venue / long venue): the short venue is the dearer one now.
    """
    days = history_days(left, right, at, cfg.get("wide_min_history_days", 30))
    times = matched(left, right, at, days * 24)
    series = [(t, math.log(right["prices"][t] / left["prices"][t])) for t in times]
    week = [x for x in series if x[0] >= hour(at) - 7 * 86400]
    xs30 = [x for t, x in series]
    xs7 = [x for t, x in week]
    mean30, sd30 = statistics.mean(xs30), statistics.pstdev(xs30)
    mean7, sd7 = statistics.mean(xs7), statistics.pstdev(xs7)
    deadband = max(0.00002, sd30 * 0.15)
    cross7, days7 = crossings(week, deadband)
    cross30, days30 = crossings(series, deadband)
    target = cfg["wide_conv_exit_bps"] / 10000
    level = max(gap * 0.5, target + 0.0003)
    closes = closing_times(series, level, target)
    return dict(
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


class EngineSource:
    """Routes market data to the native feed or the CCXT scanner."""

    def __init__(self, engine):
        self.engine = engine

    def market_rows(self):
        e = self.engine
        rows = []
        for v in NATIVE:
            try:
                fee = e.fee(v)
            except ValueError:
                continue
            for a in e.cfg["symbols"]:
                m = e.feed.meta.get(v, {}).get(a)
                if not m or m.get("rate") is None:
                    continue
                rows.append(
                    dict(
                        asset=a,
                        venue=v,
                        kind="perp",
                        mark=m["mark"],
                        rate=m["rate"],
                        raw_rate=m["rate"],
                        interval=3600,
                        next=m.get("next"),
                        received=m["received"],
                        fee=fee,
                        leverage=m.get("max_leverage"),
                        price_type="mark",
                    )
                )
        extra, _ = e.wide.snapshot()
        return rows + [
            r for r in extra if r["kind"] == "perp" and r["venue"] not in NATIVE
        ]

    def book(self, v, a):
        if v in NATIVE:
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

    def funding(self, v, a, start):
        if v in NATIVE:
            return self.engine.feed.funding_history(v, a, start)
        return self.engine.wide.funding_since(v, a, start)


# A rejected pair waits this long before it is deep-checked again, so the next ranked
# pairs get their turn; missing history is retried sooner since the keeper fills it.
REJECT_COOLDOWN = 1800
HISTORY_RETRY = 900
HISTORY_MARKERS = ("mum eksik", "işlem mumu yok", "geçmişi eksik")


def reason_group(reason):
    if any(m in reason for m in HISTORY_MARKERS):
        return "Geçmiş eksik"
    return reason


class WidePaper:
    def __init__(self, store, source, sleep=time.sleep):
        self.store = store
        self.source = source
        self.sleep = sleep
        self.cfg = {**WIDE_DEFAULTS, **store.get("wide_config", {})}
        self.positions = store.get("wide_positions", [])
        self.closed = store.get("wide_closed", [])
        self.wallet = store.get("wide_wallet", {"cash": self.cfg["wide_capital"]})
        self.manual_close = set()
        self.candidates = []
        self.assessed = []
        self.conv_candidates = []
        self.rejected = {}
        self.review = dict(rounds=0, examined=0, eligible=0, skipped=0, reasons={})
        self.rows = {}
        self.last_cycle = None
        self.error = None
        self.cache = {}
        self.latency_ms = 750
        self.extra_slippage = 0.0001

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

    def balance(self):
        pnl = sum(
            leg.get("price_pnl", 0)
            for p in self.positions
            for leg in p["legs"]
            if not leg["closed"]
        )
        margin = sum(
            leg["margin"]
            for p in self.positions
            for leg in p["legs"]
            if not leg["closed"]
        )
        equity = self.wallet["cash"] + pnl
        return dict(
            cash=self.wallet["cash"], equity=equity, margin=margin, free=equity - margin
        )

    def headroom(self):
        b = self.balance()
        return max(0, b["free"] - max(0, b["equity"]) * self.cfg["wide_reserve"])

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
            if self.positions or self.closed:
                raise ValueError(
                    "Çoklu borsa sermayesi işlem kayıtlarından sonra değiştirilemez"
                )
            self.wallet = {"cash": fresh["wide_capital"]}
            self.store.put("wide_daily", {})
        self.cfg = fresh
        self.persist()
        self.events("CONFIG", "Çoklu borsa paper ayarları güncellendi")

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

    def priced(self, c, leverage_cap, stress, size, vol=0.0, hold=24):
        """Size against margin, reserve and loss budget; VWAP all four fills on fresh books."""
        cfg = self.cfg
        a, lv, sv = c["asset"], c["long"], c["short"]
        lb, sb = self.source.book(lv, a), self.source.book(sv, a)
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
        daily = self.daily_pnl()
        if daily <= -cfg["wide_daily_loss"]:
            raise ValueError("Günlük kayıp limiti doldu")
        budget = min(cfg["wide_risk"], cfg["wide_daily_loss"] + daily)
        # Margin plus entry fee of both legs per $1 notional, at the worst accepted fill.
        per_dollar = (1 + FILL_TOLERANCE) * (2 / leverage + fee_l + fee_s)
        notional = min(
            size,
            self.headroom() / per_dollar,
            budget / max(stress + c["fee_roundtrip"] + 4 * extra, 1e-4),
        )
        price = (mid[lv] + mid[sv]) / 2
        if notional < MIN_NOTIONAL:
            raise ValueError("Risk/marjin sınırı sonrası büyüklük minimumun altında")
        # Equal base quantity on both legs.
        qty = notional / price
        la, ss = walk(lb["asks"], qty, True, extra), walk(sb["bids"], qty, False, extra)
        lc, sc = walk(lb["bids"], qty, False, extra), walk(sb["asks"], qty, True, extra)
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
                    self.funding_rows(v, c["asset"], at - 2 * 86400), iv, at
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

    def assess_funding(self, c):
        """Carry on a pair whose prices stay correlated over 7 and 30 days."""
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
            raw = self.conservative_raw(c, at)
            q = self.priced(
                c,
                cfg["wide_leverage"],
                stats["stress_fraction"],
                cfg["wide_size"],
                stats["vol"],
                cfg["wide_horizon"],
            )
            counts = c["payment_counts"]
            funding = (
                q["qty"]
                * q["price"]
                * (raw["short"] * counts[sv] - raw["long"] * counts[lv])
            )
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
            out.update(
                q,
                conservative_funding=funding,
                conservative_raw=raw,
                reversion_cost=reversion,
                risk_reserve=risk_reserve,
                net=net,
                entry_reason=(
                    "Funding: short %s, long %s; muhafazakâr %s saatlik funding $%.2f, "
                    "korelasyon 7g %.3f / %dg %.3f, fark sd %.1f bp, net $%.2f"
                    % (
                        sv,
                        lv,
                        cfg["wide_horizon"],
                        funding,
                        stats["corr"],
                        stats["history_days"],
                        stats["corr30"],
                        stats["sd"] * 10000,
                        net,
                    )
                ),
            )
            if min(stats["corr"], stats["corr30"]) < cfg["wide_min_corr"]:
                out["reasons"].append("7g / 30g getiri korelasyonu sınır altında")
            if not 0.8 <= stats["beta"] <= 1.25:
                out["reasons"].append("Hedge beta uyumsuz")
            if abs(stats["z"]) > cfg["wide_max_z"]:
                out["reasons"].append("Spread z-score sınırı aşıldı")
            if stats["sd"] > cfg["wide_max_spread_sd_bps"] / 10000:
                out["reasons"].append(
                    "Fiyat farkı istikrarlı değil (7g sd sınır üstünde)"
                )
            if funding <= 0:
                out["reasons"].append("Gerçekleşmiş funding ile kalıcı avantaj yok")
            if net < cfg["wide_min_net"]:
                out["reasons"].append(
                    "Funding geliri ücret + kayma + spread riski payını karşılamıyor"
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
            if abs(hist["mean30"]) > max(0.0002, hist["sd30"] * 0.5):
                out["reasons"].append(
                    "Kalıcı fiyat bazisi: fiyatlar sıfır çevresinde buluşmuyor"
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
            window = at + expected * 3600
            rl = dict(interval=c["interval_long"], next=c["next_long"])
            rs = dict(interval=c["interval_short"], next=c["next_short"])
            funding = q["size"] * (
                raw["short"] * payments_within(rs, at, window)
                - raw["long"] * payments_within(rl, at, window)
            )
            # Funding is not the thesis: a cost over the expected hold is charged, income is not credited.
            funding_cost = min(0.0, funding)
            target = max(hist["mean7"], 0.0)
            # Expected capture: the gap narrowing back to its 7d mean (or zero), not beyond.
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
                    "Yakınsama: long %s, short %s; boşluk %.1f bp (7g ort. %.1f, sd %.1f), "
                    "kesişim 7g %d / %dg %d, ortalama kapanma %d saat, net $%.2f"
                    % (
                        lv,
                        sv,
                        gap * 10000,
                        hist["mean7"] * 10000,
                        hist["sd7"] * 10000,
                        hist["cross7"],
                        hist["history_days"],
                        hist["cross30"],
                        expected,
                        net,
                    )
                ),
            )
            if gap < max(0.0005, hist["mean7"] + hist["sd7"]):
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

    def quote(self, v, a, qty, buy):
        b = self.source.book(v, a)
        return walk(b["asks" if buy else "bids"], qty, buy, self.extra_slippage), b

    def open(self, c):
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
        try:
            for venue, side in ((c["long"], "long"), (c["short"], "short")):
                self.sleep(self.latency_ms / 1000)
                px, b = self.quote(venue, c["asset"], c["qty"], side == "long")
                planned = c["entry_long"] if side == "long" else c["entry_short"]
                if abs(px / planned - 1) > FILL_TOLERANCE:
                    raise ValueError("Dolum öncesi fiyat %0,1 sınırını aştı")
                fee_rate = c["fee_long"] if side == "long" else c["fee_short"]
                fee = c["qty"] * px * fee_rate
                margin = c["qty"] * px / c["leverage"]
                if margin + fee > self.headroom():
                    raise ValueError("Dolum öncesi marjin rezervi yetersiz")
                interval = c["interval_long"] if side == "long" else c["interval_short"]
                nxt = c["next_long"] if side == "long" else c["next_short"]
                entered = now()
                leg = dict(
                    venue=venue,
                    side=side,
                    qty=c["qty"],
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
                    audit_id=b.get("audit_id"),
                )
                p["legs"].append(leg)
                p["fees"] += fee
                self.wallet["cash"] -= fee
                self.persist()
                self.events("FILL", "Sanal bacak doldu", position=p["id"], leg=leg)
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
                    self.wallet["cash"] += cash
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
                                    dict(payment_time=tm, venue=v, recorded=now())
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

    def mark_position(self, p, books):
        unrealized = exit_fee = 0.0
        for leg in p["legs"]:
            if leg["closed"]:
                continue
            b = books[leg["venue"]]
            buy = leg["side"] == "short"
            px = walk(
                b["asks" if buy else "bids"], leg["qty"], buy, self.extra_slippage
            )
            leg["price_pnl"] = (
                leg["qty"] * (px - leg["entry"]) * (1 if leg["side"] == "long" else -1)
            )
            leg["exit_estimate"] = px
            unrealized += leg["price_pnl"]
            exit_fee += leg["qty"] * px * leg["fee_rate"]
        p["unrealized"] = unrealized - exit_fee
        p["price_pnl_open"] = unrealized
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
                self.sleep(self.latency_ms / 1000)
                px, b = self.quote(
                    leg["venue"], p["asset"], leg["qty"], leg["side"] == "short"
                )
                pnl = (
                    leg["qty"]
                    * (px - leg["entry"])
                    * (1 if leg["side"] == "long" else -1)
                )
                fee = leg["qty"] * px * leg["fee_rate"]
                self.wallet["cash"] += pnl - fee
                p["price_pnl"] += pnl
                p["fees"] += fee
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
        held = now() - p["opened"]
        if p.get("strategy") == "convergence":
            mid = {
                v: (max(x for x, q in b["bids"]) + min(x for x, q in b["asks"])) / 2
                for v, b in books.items()
            }
            gap = math.log(mid[d["short"]] / mid[d["long"]])
            p["live_gap"] = gap
            zone = max(d.get("target_gap", 0.0), cfg["wide_conv_exit_bps"] / 10000)
            # No need to wait for an exact cross: in the mean/cross zone the
            # after-close net decides.
            if gap <= zone and p["net_if_closed"] >= cfg["wide_min_net"]:
                return "Fark ortalama/kesişim bölgesine geldi; kapanış sonrası net hedef sağlandı"
            if gap <= 0 and p["net_if_closed"] > 0:
                return "Fiyatlar kesişti"
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
            if (
                min(stats["corr"], stats["corr30"]) < cfg["wide_min_corr"]
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
            flipped = ahead < 0
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
        for key in ("funding", "convergence"):
            done = [p for p in self.closed if p.get("strategy", "funding") == key]
            settled = [p for p in done if not p.get("pending_funding")]
            out[key] = dict(
                open=sum(p.get("strategy", "funding") == key for p in self.positions),
                trades=len(done),
                settled=len(settled),
                wins=sum(p["net"] > 0 for p in settled),
                net=sum(p["net"] for p in settled),
                price_pnl=sum(p["price_pnl"] for p in settled),
                funding=sum(p["funding"] for p in settled),
                estimated_funding=sum(p.get("estimated_funding", 0) for p in settled),
                fees=sum(p["fees"] for p in settled),
                unverified_payments=sum(
                    len(p.get("funding_unverified", [])) for p in done
                ),
                pending=len(done) - len(settled),
            )
        return out

    def cycle(self, entries=True):
        self.error = None
        try:
            try:
                rows = self.source.market_rows()
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
                        l["venue"]: self.source.book(l["venue"], p["asset"])
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
            at = now()
            self.candidates = (
                rank(rows, self.cfg, at)
                if rows is not None and self.cfg["wide_funding_enabled"]
                else []
            )
            self.conv_candidates = (
                rank_spreads(rows, at)
                if rows is not None and self.cfg["wide_conv_enabled"]
                else []
            )
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
                for ranked in (self.candidates, self.conv_candidates):
                    picked = []
                    for c in ranked:
                        if len(picked) >= self.cfg["wide_deep_top"] or c["score"] <= 0:
                            break
                        if (
                            self.blocked(c)
                            or any(x["asset"] == c["asset"] for x in picked)
                            or self.pair_key(c) in self.rejected
                        ):
                            self.review["skipped"] += 1
                            continue
                        picked.append(self.assess(c))
                    self.assessed += picked
                for x in self.assessed:
                    self.note_review(x, at)
                best = max(
                    (x for x in self.assessed if x["eligible"]),
                    key=lambda x: x["net"],
                    default=None,
                )
                if best:
                    stale = self.entry_funding_check(best)
                    if stale:
                        best["eligible"] = False
                        best["reasons"].append(stale)
                        self.review["eligible"] -= 1
                        self.reject(best, [stale], at)
                        self.events(
                            "ENTRY_SKIP",
                            stale,
                            asset=best["asset"],
                            strategy=best["strategy"],
                        )
                    else:
                        self.open(best)
            self.last_cycle = now()
            self.persist()
        except Exception as ex:
            self.error = str(ex)
            self.events("ERROR", str(ex))

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

    def reject(self, x, reasons, at):
        groups = list(dict.fromkeys(reason_group(t) for t in reasons))
        for g in groups:
            self.review["reasons"][g] = self.review["reasons"].get(g, 0) + 1
        self.rejected[self.pair_key(x)] = dict(
            strategy=x.get("strategy") or "funding",
            asset=x["asset"],
            long=x["long"],
            short=x["short"],
            reason=reasons[0],
            group=groups[0],
            at=at,
            until=at
            + (HISTORY_RETRY if groups[0] == "Geçmiş eksik" else REJECT_COOLDOWN),
        )

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
            daily_pnl=self.daily_pnl(),
            positions=self.positions,
            closed=self.closed[-100:],
            report=self.report(),
            candidates=self.candidates[:40],
            candidate_count=len(self.candidates),
            conv_candidates=self.conv_candidates[:40],
            conv_candidate_count=len(self.conv_candidates),
            assessed=[{k: v for k, v in c.items()} for c in self.assessed],
            review=self.review_snapshot(),
            venues=venues,
            last_cycle=self.last_cycle,
            error=self.error,
            method=(
                "İki strateji, taranan tüm borsa çiftleri; net = iki bacağın fiyat PnL'si + net funding − "
                "komisyon, kayma dolum fiyatlarında. Funding: 7g/30g korele ve farkı istikrarlı çiftler, "
                "aralıkları eşitlenmiş short − long funding; ücret, kayma ve fark riski payını karşılarsa "
                "açılır; zayıflık kalıcıysa, uyum bozulursa veya bacak marjini riske girerse çıkılır. "
                "Yakınsama: 7g ve 30g düzenli kesişim ve kapanma süresi; ucuzda long, pahalıda short, eşit "
                "miktar, en fazla 5x; ortalama/kesişim bölgesinde kapanış sonrası nete bakılarak kapanır. "
                "Kaldıraç tavandır, her bacağın kendi marjinine göre küçülür. Gerçek emir ve API anahtarı yok."
            ),
        )
