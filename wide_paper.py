"""Multi-venue paper desk: ranks every scanned perp pair and paper-trades the best.

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
)
LIMITS = dict(
    wide_capital=(10, 1000000),
    wide_size=(10, 100000),
    wide_leverage=(1, 10),
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
)
HISTORY_TTL = 1800
FUNDING_TTL = 120
MIN_NOTIONAL = 10


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
    usable = [
        r
        for r in rows
        if r.get("kind", "perp") == "perp"
        and r.get("rate") is not None
        and r.get("raw_rate") is not None
        and r.get("interval")
        and r.get("fee") is not None
        and (r.get("mark") or 0) > 0
        and r.get("price_type") != "last_trade"
        and at - r["received"] <= 180
    ]
    groups = {}
    for r in usable:
        groups.setdefault(r["asset"], {})
        # One market per venue and asset.
        groups[r["asset"]].setdefault(r["venue"], r)
    out = []
    for a, by_venue in groups.items():
        for x, y in itertools.combinations(by_venue.values(), 2):
            l, s = sorted((x, y), key=lambda r: (r["rate"], r["venue"]))
            if s["rate"] - l["rate"] <= 0:
                continue
            nl, ns = payments_within(l, at, end), payments_within(s, at, end)
            funding = s["raw_rate"] * ns - l["raw_rate"] * nl
            fees = 2 * (l["fee"] + s["fee"])
            out.append(
                dict(
                    asset=a,
                    long=l["venue"],
                    short=s["venue"],
                    funding_gap=s["rate"] - l["rate"],
                    funding_horizon=funding,
                    fee_roundtrip=fees,
                    spread=math.log(s["mark"] / l["mark"]),
                    score=funding - fees,
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
                    payment_counts={l["venue"]: nl, s["venue"]: ns},
                )
            )
    out.sort(key=lambda c: (-c["score"], c["asset"], c["long"], c["short"]))
    for i, c in enumerate(out):
        c["rank"] = i + 1
    return out


def pair_stats(left, right, at, horizon):
    """7-day hedge quality on matched closed 1h trade candles."""
    if left.get("price_type") != "trade" or right.get("price_type") != "trade":
        raise ValueError("Gerçek işlem mumu yok; oracle/eksik seri ile giriş yapılmaz")
    end = hour(at)
    times = [
        t
        for t in range(end - 168 * 3600, end, 3600)
        if t in left["prices"] and t in right["prices"]
    ]
    if len(times) < 160:
        raise ValueError("7 günlük eşlenmiş 1h mum eksik")
    a = [left["prices"][t] for t in times]
    b = [right["prices"][t] for t in times]
    ra = [math.log(a[i] / a[i - 1]) for i in range(1, len(a))]
    rb = [math.log(b[i] / b[i - 1]) for i in range(1, len(b))]
    c, beta = corr(ra, rb)
    spread = [math.log(y / x) for x, y in zip(a, b)]
    mean, sd = statistics.mean(spread), statistics.pstdev(spread)
    residual = statistics.pstdev([x - y for x, y in zip(ra, rb)])
    return dict(
        corr=c,
        beta=beta,
        mean=mean,
        sd=sd,
        z=(spread[-1] - mean) / sd if sd > 1e-10 else 0.0,
        stress_fraction=3 * residual * math.sqrt(horizon),
        points=len(times),
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
        if v in NATIVE:
            return {**self.engine.feed.candles(v, a), "price_type": "trade"}
        prices, _, _ = self.engine.wide.history(v, a, 7)
        return prices

    def funding(self, v, a, start):
        if v in NATIVE:
            return self.engine.feed.funding_history(v, a, start)
        return self.engine.wide.funding_since(v, a, start)


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
            if k == "wide_enabled":
                if not isinstance(v, bool):
                    raise ValueError("wide_enabled true/false olmalı")
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

    def assess(self, c):
        """Fresh books, 7d hedge quality and 24h realized funding for one ranked pair."""
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
            )
            out["stats"] = stats
            raw = {}
            for v, side, iv, current in (
                (lv, "long", c["interval_long"], c["raw_long"]),
                (sv, "short", c["interval_short"], c["raw_short"]),
            ):
                realized = realized_raw(self.funding_rows(v, a, at - 2 * 86400), iv, at)
                if realized is None:
                    raise ValueError(v + ": 24 saat gerçekleşmiş funding geçmişi eksik")
                # The long pays the higher of current/realized, the short earns the lower.
                raw[side] = (
                    max(current, realized) if side == "long" else min(current, realized)
                )
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
            leverage = min(
                [cfg["wide_leverage"]]
                + [x for x in (c.get("leverage_long"), c.get("leverage_short")) if x]
            )
            fee_l, fee_s = c["fee_long"], c["fee_short"]
            extra = self.extra_slippage
            stress = stats["stress_fraction"]
            daily = self.daily_pnl()
            if daily <= -cfg["wide_daily_loss"]:
                raise ValueError("Günlük kayıp limiti doldu")
            budget = min(cfg["wide_risk"], cfg["wide_daily_loss"] + daily)
            # Margin plus entry fee of both legs per $1 notional, at the worst accepted fill.
            per_dollar = (1 + FILL_TOLERANCE) * (2 / leverage + fee_l + fee_s)
            notional = min(
                cfg["wide_size"],
                self.headroom() / per_dollar,
                budget / max(stress + c["fee_roundtrip"] + 4 * extra, 1e-4),
            )
            price = (mid[lv] + mid[sv]) / 2
            if notional < MIN_NOTIONAL:
                raise ValueError(
                    "Risk/marjin sınırı sonrası büyüklük minimumun altında"
                )
            qty = notional / price
            la, ss = walk(lb["asks"], qty, True, extra), walk(
                sb["bids"], qty, False, extra
            )
            lc, sc = walk(lb["bids"], qty, False, extra), walk(
                sb["asks"], qty, True, extra
            )
            entry_fee = qty * (la * fee_l + ss * fee_s)
            exit_fee = qty * (lc * fee_l + sc * fee_s)
            drag = qty * ((la - lc) + (sc - ss))
            counts = c["payment_counts"]
            funding = (
                qty * price * (raw["short"] * counts[sv] - raw["long"] * counts[lv])
            )
            spread = math.log(mid[sv] / mid[lv])
            # Short the rich leg: reverting to the 7d mean only costs when we enter below it.
            reversion = qty * price * min(0.0, spread - stats["mean"])
            net = funding - entry_fee - exit_fee - drag + reversion
            stress_loss = qty * price * stress
            out.update(
                qty=qty,
                size=qty * price,
                leverage=leverage,
                entry_long=la,
                entry_short=ss,
                conservative_funding=funding,
                conservative_raw=raw,
                entry_fee=entry_fee,
                exit_fee=exit_fee,
                execution_drag=drag,
                reversion_cost=reversion,
                stress_loss=stress_loss,
                live_spread=spread,
                net=net,
                audit_ids=[lb.get("audit_id"), sb.get("audit_id")],
            )
            if stats["corr"] < cfg["wide_min_corr"]:
                out["reasons"].append("7g getiri korelasyonu sınır altında")
            if not 0.8 <= stats["beta"] <= 1.25:
                out["reasons"].append("Hedge beta uyumsuz")
            if abs(stats["z"]) > cfg["wide_max_z"]:
                out["reasons"].append("Spread z-score sınırı aşıldı")
            if funding <= 0:
                out["reasons"].append("Gerçekleşmiş funding ile kalıcı avantaj yok")
            if net < cfg["wide_min_net"]:
                out["reasons"].append(
                    "Funding geliri ücret + kayma + spread maliyetini karşılamıyor"
                )
            if entry_fee + exit_fee + drag + stress_loss > budget + 1e-6:
                out["reasons"].append("İşlem kayıp bütçesi aşılıyor")
        except Exception as ex:
            out["reasons"].append(str(ex))
        out["reasons"] = list(dict.fromkeys(out["reasons"]))
        out["eligible"] = not out["reasons"]
        return out

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
            strategy="wide_funding",
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

    def exit_reason(self, p):
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
        if now() - p["opened"] > cfg["wide_max_hold_hours"] * 3600:
            return "Azami tutma süresi"
        l, s = self.rows.get((d["long"], p["asset"])), self.rows.get(
            (d["short"], p["asset"])
        )
        if l and s and l.get("raw_rate") is not None and s.get("raw_rate") is not None:
            end = now() + cfg["wide_horizon"] * 3600
            ahead = s["raw_rate"] * payments_within(s, now(), end) - l[
                "raw_rate"
            ] * payments_within(l, now(), end)
            p["remaining_funding_fraction"] = ahead
            # Exit fees are owed either way; leave once future carry turns negative.
            if ahead < 0:
                return "Funding avantajı tersine döndü"
        return None

    def cycle(self, entries=True):
        self.error = None
        try:
            rows = self.source.market_rows()
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
                    reason = self.exit_reason(p)
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
                if p.get("pending_funding"):
                    self.settle(p)
                    p["net"] = p["price_pnl"] + p["funding"] - p["fees"]
            self.candidates = rank(rows, self.cfg, now())
            self.assessed = []
            if (
                entries
                and self.cfg["wide_enabled"]
                and len(self.positions) < self.cfg["wide_max_positions"]
                and not any(p["status"] != "OPEN" for p in self.positions)
            ):
                for c in self.candidates:
                    if len(self.assessed) >= self.cfg["wide_deep_top"]:
                        break
                    if c["score"] <= 0:
                        break
                    if self.blocked(c):
                        continue
                    if any(x["asset"] == c["asset"] for x in self.assessed):
                        continue
                    self.assessed.append(self.assess(c))
                best = max(
                    (x for x in self.assessed if x["eligible"]),
                    key=lambda x: x["net"],
                    default=None,
                )
                if best:
                    self.open(best)
            self.last_cycle = now()
            self.persist()
        except Exception as ex:
            self.error = str(ex)
            self.events("ERROR", str(ex))

    def snapshot(self):
        venues = sorted({v for v, a in self.rows})
        return dict(
            config=self.cfg,
            balance=self.balance(),
            daily_pnl=self.daily_pnl(),
            positions=self.positions,
            closed=self.closed[-100:],
            candidates=self.candidates[:40],
            candidate_count=len(self.candidates),
            assessed=[{k: v for k, v in c.items()} for c in self.assessed],
            venues=venues,
            last_cycle=self.last_cycle,
            error=self.error,
            method=(
                "Tüm taranan borsa çiftleri: her borsanın kendi funding aralığı ve sonraki ödeme saatiyle "
                "horizon funding'i − 4 taker ücreti sıralanır. İlk adaylarda güncel defter VWAP'ı, 7g fiyat "
                "korelasyonu/beta/z ve 24s gerçekleşmiş funding (kötü olan kullanılır) kontrol edilir; ters "
                "spread maliyet sayılır. Tek ortak paper cüzdan; gerçek emir ve API anahtarı yok."
            ),
        )
