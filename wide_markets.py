"""Public-only venue coverage. Scan adapters never place orders or accept credentials."""

import concurrent.futures, datetime, email.utils, itertools, json, math, re, sys, threading, time
from pathlib import Path
from bot import now, hour, number
from risk_scan import VENUES, DISPLAY

try:
    import ccxt
except ImportError:
    ccxt = None

# The reference's full venue inventory, including venues whose APIs remain unverified.
SPECS = [
    ("Aevo", "DEX", None),
    ("Aftermath", "DEX", None),
    ("ApeX", "DEX", "apex"),
    ("Arcus", "DEX", None),
    ("Aster", "DEX", "aster"),
    ("Avantis", "DEX", None),
    ("AZverse", "DEX", None),
    ("Backpack", "CEX", "backpack"),
    ("Binance", "CEX", "binanceusdm"),
    ("BingX", "CEX", "bingx"),
    ("Bitget", "CEX", "bitget"),
    ("BloFin", "CEX", "blofin"),
    ("Bluefin", "DEX", None),
    ("Bulk", "DEX", None),
    ("Bullet", "DEX", None),
    ("Bybit", "CEX", "bybit"),
    ("Deribit", "CEX", "deribit"),
    ("Derive", "DEX", "derive"),
    ("dYdX", "DEX", "dydx"),
    ("edgeX", "DEX", None),
    ("Entropy", "DEX", None),
    ("Extended", "DEX", None),
    ("Gate", "CEX", "gate"),
    ("GMX", "DEX", None),
    ("GRVT", "DEX", "grvt"),
    ("gTrade", "DEX", None),
    ("Hibachi", "DEX", "hibachi"),
    ("Hotstuff", "DEX", None),
    ("HTX", "CEX", "htx"),
    ("Hyperliquid", "DEX", "hyperliquid"),
    ("Injective", "DEX", None),
    ("JupiterPerps", "DEX", None),
    ("Kalshi", "DEX", None),
    ("KatanaPerps", "DEX", None),
    ("KrakenFutures", "CEX", "krakenfutures"),
    ("KuCoin", "CEX", "kucoinfutures"),
    ("LighterCore", "DEX", "lighter"),
    ("Lighter", "DEX", None),
    ("Kinetiq", "DEX", None),
    ("MEXC", "CEX", "mexc"),
    ("N1", "DEX", None),
    ("Nado", "DEX", "nado"),
    ("OKX", "CEX", "okx"),
    ("OndoPerps", "DEX", None),
    ("Orderly", "DEX", None),
    ("Pacifica", "DEX", "pacifica"),
    ("Paradex", "DEX", "paradex"),
    ("Paragon", "DEX", None),
    ("Perpl", "DEX", None),
    ("Phemex", "CEX", "phemex"),
    ("Phoenix", "DEX", None),
    ("PolymarketPerps", "DEX", None),
    ("Popdex", "DEX", None),
    ("QFEX", "DEX", None),
    ("Reya", "DEX", None),
    ("RISEx", "DEX", None),
    ("SoDEX", "DEX", None),
    ("StandX", "DEX", None),
    ("SunX", "DEX", None),
    ("tradeXYZ", "DEX", None),
    ("TxFlow", "DEX", None),
    ("Variational", "DEX", None),
    ("Vest", "DEX", None),
]
LABELS = {
    **{v: v for v, k, a in SPECS},
    **DISPLAY,
    "LighterCore": "Lighter Core",
    "KrakenFutures": "Kraken Futures",
    "JupiterPerps": "Jupiter Perps",
    "KatanaPerps": "Katana Perps",
    "Kinetiq": "Markets by Kinetiq",
    "OndoPerps": "Ondo Perps",
    "PolymarketPerps": "Polymarket Perps",
    "Variational": "Variational Omni",
    "Vest": "Vest Markets",
    "gTrade": "gTrade (Gains Network)",
    "ApeX": "ApeX Omni",
    "Aftermath": "Aftermath Perps",
}
CATALOG = {
    v: dict(
        id=v,
        label=LABELS[v],
        kind=k,
        adapter=a,
        native=v in VENUES,
        scan=bool(a or v in VENUES),
        paper=v in ("Lighter", "Extended"),
        backtest=bool(a or v in ("Lighter", "Extended", "RISEx")),
        note=(
            "Özel public adaptör bağlı"
            if v in VENUES
            else (
                "Public adaptör mevcut; erişim testi bekleniyor"
                if a
                else "Doğrulanmış public adaptör eksik; tarama ve işlem dışında"
            )
        ),
    )
    for v, k, a in SPECS
}
SPOT_ADAPTERS = {"Binance": "binance", "KuCoin": "kucoin", "KrakenFutures": "kraken"}


def finite(x):
    try:
        n = float(x)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def comparable_prices(first, second, at):
    return (
        first.get("price_type") != "last_trade"
        and second.get("price_type") != "last_trade"
        and abs(first["received"] - second["received"]) <= 30
        and at - min(first["received"], second["received"]) <= 90
    )


def funding_interval(rate, market, previous=None):
    """No guessed 8h default: rate periods must come from source metadata."""
    val = rate.get("interval")
    if isinstance(val, str):
        m = re.fullmatch(r"(\d+(?:\.\d+)?)([hms])", val)
        if m:
            return float(m[1]) * {"h": 3600, "m": 60, "s": 1}[m[2]]
    current, nxt = finite(rate.get("fundingTimestamp")), finite(
        rate.get("nextFundingTimestamp")
    )
    if current and nxt and 0 < nxt - current <= 86400000:
        return (nxt - current) / 1000
    raw = market.get("info", {})
    info = rate.get("info", {})
    for d in (info, raw):
        for key, factor in [
            ("fundingIntervalHour", 3600),
            ("fundingIntervalHours", 3600),
            ("fundingInterval", 60),
            ("funding_interval", 1),
            ("fundingRateGranularity", 0.001),
        ]:
            value = finite(d.get(key))
            if value and 0 < value * factor <= 86400:
                return value * factor
    return previous


def public_error(ex):
    # No user credentials are configured in any scan client.
    return (type(ex).__name__ + ": " + str(ex)).replace("\n", " ")[:350]


class WideMarkets:
    def __init__(self, store, symbols, stop):
        self.store = store
        self.symbols = list(symbols)
        self.stop = stop
        self.lock = threading.RLock()
        self.clients = {}
        self.client_locks = {v: threading.RLock() for v in CATALOG}
        self.rows = {}
        self.status = {}
        self.last_scan = None
        self.thread = None
        self.histories = {}

    def client(self, v, spot=False):
        key = v + ":spot" if spot else v
        if key in self.clients:
            return self.clients[key]
        spec = CATALOG[v]
        adapter = SPOT_ADAPTERS.get(v) if spot else spec["adapter"]
        if not adapter or not ccxt or not hasattr(ccxt, adapter):
            raise ValueError("Public adaptör bulunmuyor")
        ex = getattr(ccxt, adapter)(
            {
                "enableRateLimit": True,
                "timeout": 6000,
                "options": {"defaultType": "spot" if spot else "swap"},
            }
        )
        original = ex.fetch

        def audited(url, method="GET", headers=None, body=None):
            result = original(url, method, headers, body)
            received = now()
            h = ex.last_response_headers or {}
            date = h.get("Date") or h.get("date")
            age = finite(h.get("Age", h.get("age", 0))) or 0
            if date:
                try:
                    age = max(
                        age,
                        received - email.utils.parsedate_to_datetime(date).timestamp(),
                    )
                except (ValueError, TypeError):
                    pass
            if age > 120 and getattr(ex, "neutralbasis_live", False):
                raise ValueError("HTTP verisi eski; taramaya dahil edilmedi")
            ex.neutralbasis_audit = self.store.audit(
                url,
                (ex.last_http_response or json.dumps(result)).encode(),
                received,
                dict(h),
            )
            return result

        ex.fetch = audited
        ex.neutralbasis_audit = None
        ex.load_markets()
        self.clients[key] = ex
        return ex

    def scan_one(self, v):
        try:
            with self.client_locks[v]:
                ex = self.client(v)
                ex.neutralbasis_live = True
                selected = {}
                for m in ex.markets.values():
                    if (
                        m.get("base") not in self.symbols
                        or m.get("active") is False
                        or not m.get("swap")
                        or not m.get("linear")
                        or m.get("quote") not in ("USDT", "USDC", "USD", "USDG")
                    ):
                        continue
                    a = m["base"]
                    old = selected.get(a)
                    if old is None or (m["quote"] == "USDT" and old["quote"] != "USDT"):
                        selected[a] = m
                if not selected:
                    raise ValueError(
                        "Seçili varlıklarda doğrulanmış lineer perpetual yok"
                    )
                symbols = [m["symbol"] for m in selected.values()]
                if ex.has.get("fetchFundingRates"):
                    try:
                        rates = ex.fetch_funding_rates(symbols)
                    except ccxt.NotSupported:
                        rates = (
                            {s: ex.fetch_funding_rate(s) for s in symbols}
                            if ex.has.get("fetchFundingRate")
                            else {}
                        )
                elif ex.has.get("fetchFundingRate"):
                    rates = {s: ex.fetch_funding_rate(s) for s in symbols}
                else:
                    rates = {}
                rates_received = now()
                rate_audit = getattr(ex, "neutralbasis_audit", None)
                period_meta = {}
                marks = {}
                open_interest = {}
                if ex.has.get("fetchFundingIntervals"):
                    try:
                        period_meta = ex.fetch_funding_intervals(symbols)
                    except Exception:
                        pass
                if ex.has.get("fetchMarkPrices"):
                    try:
                        marks = ex.fetch_mark_prices(symbols)
                    except Exception:
                        pass
                marks_received = now()
                if ex.has.get("fetchOpenInterests"):
                    try:
                        open_interest = ex.fetch_open_interests(symbols)
                    except Exception:
                        pass
                elif ex.has.get("fetchOpenInterest"):
                    for symbol in symbols:
                        try:
                            open_interest[symbol] = ex.fetch_open_interest(symbol)
                        except Exception:
                            break
                try:
                    ticks = (
                        ex.fetch_tickers(symbols) if ex.has.get("fetchTickers") else {}
                    )
                except Exception:
                    ticks = {}
                received = now()
                out = []
                inferred = {}
                for a, m in selected.items():
                    r = rates.get(m["symbol"], {})
                    if funding_interval(
                        r, m, funding_interval(period_meta.get(m["symbol"], {}), m)
                    ) is None and ex.has.get("fetchFundingRateHistory"):
                        try:
                            recent = ex.fetch_funding_rate_history(
                                m["symbol"], int((now() - 86400) * 1000), 10
                            )
                            times = sorted(
                                set(
                                    x["timestamp"]
                                    for x in recent
                                    if x.get("timestamp")
                                    and x.get("timestamp") <= now() * 1000
                                )
                            )
                            diffs = [b - a for a, b in zip(times, times[1:])]
                            if (
                                len(diffs) >= 2
                                and len(set(diffs)) == 1
                                and 0 < diffs[0] <= 86400000
                            ):
                                inferred[m["symbol"]] = diffs[0] / 1000
                        except Exception:
                            pass
                for a, m in selected.items():
                    r = rates.get(m["symbol"], {})
                    t = ticks.get(m["symbol"], {})
                    interval = funding_interval(
                        r,
                        m,
                        funding_interval(
                            period_meta.get(m["symbol"], {}),
                            m,
                            inferred.get(m["symbol"]),
                        ),
                    )
                    raw_rate = finite(r.get("fundingRate"))
                    mark = finite(r.get("markPrice"))
                    ptype = "mark"
                    price_received = rates_received
                    if not mark:
                        mark = finite(
                            marks.get(m["symbol"], {}).get("markPrice")
                            or marks.get(m["symbol"], {}).get("last")
                        )
                        price_received = marks_received
                    if not mark:
                        mark = finite(t.get("info", {}).get("markPrice"))
                        price_received = received
                    if not mark:
                        bid, ask = finite(t.get("bid")), finite(t.get("ask"))
                        if bid and ask and 0 < bid < ask:
                            mark = (bid + ask) / 2
                            ptype = "ticker_mid"
                            price_received = received
                        else:
                            mark = finite(t.get("last"))
                            ptype = "last_trade"
                            price_received = received
                    if not mark and ex.has.get("fetchTicker"):
                        try:
                            t = ex.fetch_ticker(m["symbol"])
                            mark = finite(t.get("last"))
                        except Exception:
                            pass
                    if not mark or mark <= 0:
                        continue
                    source_ts = finite(t.get("timestamp") or r.get("timestamp"))
                    stale = (
                        source_ts is not None and abs(received - source_ts / 1000) > 180
                    )
                    if stale:
                        continue
                    next_time = finite(
                        r.get("nextFundingTimestamp") or r.get("fundingTimestamp")
                    )
                    if (
                        next_time
                        and interval
                        and next_time / 1000 < received - interval
                    ):
                        raw_rate = None
                    oi = finite(
                        open_interest.get(m["symbol"], {}).get("openInterestValue")
                    )
                    out.append(
                        dict(
                            asset=a,
                            venue=v,
                            kind="perp",
                            symbol=m["symbol"],
                            mark=mark,
                            price_type=ptype,
                            rate=(
                                raw_rate / (interval / 3600)
                                if raw_rate is not None and interval
                                else None
                            ),
                            raw_rate=raw_rate,
                            interval=interval,
                            next=next_time / 1000 if next_time else None,
                            received=price_received,
                            funding_received=rates_received,
                            source_timestamp=source_ts,
                            fee=finite(m.get("taker")),
                            leverage=finite(
                                m.get("limits", {}).get("leverage", {}).get("max")
                            ),
                            volume=finite(t.get("quoteVolume")),
                            oi=oi,
                            basis="CCXT normalized public funding / API payment interval",
                            audit_id=rate_audit,
                            execution=False,
                            quote=m["quote"],
                        )
                    )
                if not out:
                    raise ValueError("Fiyat snapshot’ı yok; eski veya eksik veri")
                # Spot cannot be shorted in the simulator. It is only a long cash leg.
                try:
                    spot_ex = self.client(v, True) if v in SPOT_ADAPTERS else ex
                    spot_ex.neutralbasis_live = True
                except Exception:
                    spot_ex = ex
                spots = {
                    a: next(
                        (
                            m
                            for m in spot_ex.markets.values()
                            if m.get("spot")
                            and m.get("base") == a
                            and m.get("quote") in ("USDT", "USDC", "USD")
                            and m.get("active") is not False
                        ),
                        None,
                    )
                    for a in self.symbols
                }
                spots = {a: m for a, m in spots.items() if m}
                if spots and spot_ex.has.get("fetchTickers"):
                    try:
                        st = spot_ex.fetch_tickers(
                            [m["symbol"] for m in spots.values()]
                        )
                        for a, m in spots.items():
                            t = st.get(m["symbol"], {})
                            bid, ask = finite(t.get("bid")), finite(t.get("ask"))
                            if bid and ask and 0 < bid < ask:
                                out.append(
                                    dict(
                                        asset=a,
                                        venue=v,
                                        kind="spot",
                                        symbol=m["symbol"],
                                        mark=ask,
                                        price_type="best_ask",
                                        rate=0.0,
                                        raw_rate=0.0,
                                        interval=0,
                                        next=None,
                                        received=now(),
                                        source_timestamp=t.get("timestamp"),
                                        fee=finite(m.get("taker")),
                                        leverage=1,
                                        volume=finite(t.get("quoteVolume")),
                                        oi=None,
                                        basis="Spot alış kotasyonu; funding yok",
                                        audit_id=spot_ex.neutralbasis_audit,
                                        execution=False,
                                        quote=m["quote"],
                                    )
                                )
                    except Exception:
                        pass
                with self.lock:
                    self.rows[v] = out
                    self.status[v] = dict(
                        ok=True,
                        status="ready",
                        ts=now(),
                        markets=len([r for r in out if r["kind"] == "perp"]),
                        spots=len([r for r in out if r["kind"] == "spot"]),
                        message="Gerçek public veri; hesap anahtarı kullanılmıyor",
                        docs=ex.urls.get("doc"),
                        funding_periods=sorted(
                            set(r["interval"] for r in out if r["interval"])
                        ),
                    )
        except Exception as ex:
            with self.lock:
                self.status[v] = dict(
                    ok=False, status="unavailable", ts=now(), message=public_error(ex)
                )

    def start(self):
        if self.thread:
            return
        semaphore = threading.BoundedSemaphore(5)
        self.threads = []
        self.thread = True

        def loop(v):
            while not self.stop.is_set():
                with semaphore:
                    if self.stop.is_set():
                        return
                    self.scan_one(v)
                    self.last_scan = now()
                self.stop.wait(60)

        for v, s in CATALOG.items():
            if s["adapter"]:
                thread = threading.Thread(
                    target=loop, args=(v,), daemon=True, name="public-" + v
                )
                self.threads.append(thread)
                thread.start()

    def snapshot(self):
        with self.lock:
            return [
                dict(r)
                for rs in self.rows.values()
                for r in rs
                if now() - r["received"] <= 180
            ], {v: dict(s) for v, s in self.status.items()}

    def history(self, v, a, days, kind="perp", at=None):
        with self.client_locks[v]:
            ex = self.client(v, kind == "spot" and v in SPOT_ADAPTERS)
            ex.neutralbasis_live = False
            end = hour(at if at is not None else now())
            start = end - (int(days) * 24 + 1) * 3600
            with self.lock:
                row = next(
                    (
                        r
                        for r in self.rows.get(v, [])
                        if r["asset"] == a and r["kind"] == kind
                    ),
                    None,
                )
            if not row:
                raise ValueError(v + ": seçili piyasanın güncel verisi yok")
            if not ex.has.get("fetchOHLCV"):
                raise ValueError(v + ": gerçek işlem mumları adaptörde yok")
            cursor = start * 1000
            prices = {}
            audits = []
            for _ in range(30):
                batch = ex.fetch_ohlcv(row["symbol"], "1h", cursor, 500)
                audits.append(ex.neutralbasis_audit)
                if not batch:
                    break
                for x in batch:
                    t = int(x[0] / 1000)
                    p = finite(x[4])
                    if start <= t < end and p and p > 0:
                        prices[t] = p
                nxt = max(x[0] for x in batch) + 3600000
                if nxt <= cursor or nxt >= end * 1000:
                    break
                cursor = nxt
            rates = []
            if kind != "spot":
                if not ex.has.get("fetchFundingRateHistory"):
                    raise ValueError(v + ": gerçekleşmiş funding geçmişi adaptörde yok")
                cursor = (start + 3600) * 1000
                for _ in range(40):
                    batch = ex.fetch_funding_rate_history(row["symbol"], cursor, 200)
                    audit = ex.neutralbasis_audit
                    if not batch:
                        break
                    for r in batch:
                        t = finite(r.get("timestamp"))
                        value = finite(r.get("fundingRate"))
                        if (
                            t is not None
                            and value is not None
                            and cursor <= t <= end * 1000
                        ):
                            rates.append(
                                dict(
                                    ts=t / 1000, rate=value, value=None, audit_id=audit
                                )
                            )
                    nxt = max((r.get("timestamp") or 0) for r in batch) + 1
                    if nxt <= cursor or nxt >= end * 1000:
                        break
                    cursor = nxt
                rates = list({r["ts"]: r for r in rates}.values())
            result = dict(prices=prices, audit_ids=audits, end=end, price_type="trade")
            with self.lock:
                self.histories[(v, a, kind)] = dict(
                    prices=result, rates=rates, interval=row["interval"], ts=now()
                )
            return result, sorted(rates, key=lambda r: r["ts"]), row

    def funding_since(self, v, a, start):
        """Realized per-interval funding rates after start; used to settle paper legs."""
        with self.client_locks[v]:
            ex = self.client(v)
            ex.neutralbasis_live = False
            with self.lock:
                row = next(
                    (
                        r
                        for r in self.rows.get(v, [])
                        if r["asset"] == a and r["kind"] == "perp"
                    ),
                    None,
                )
            if not row:
                raise ValueError(v + ": seçili piyasanın güncel verisi yok")
            if not ex.has.get("fetchFundingRateHistory"):
                raise ValueError(v + ": gerçekleşmiş funding geçmişi adaptörde yok")
            cursor = int(start * 1000)
            rates = []
            for _ in range(8):
                batch = ex.fetch_funding_rate_history(row["symbol"], cursor, 200)
                audit = ex.neutralbasis_audit
                if not batch:
                    break
                for r in batch:
                    t = finite(r.get("timestamp"))
                    value = finite(r.get("fundingRate"))
                    if t is not None and value is not None and t >= cursor:
                        rates.append(
                            dict(ts=t / 1000, rate=value, value=None, audit_id=audit)
                        )
                nxt = max((r.get("timestamp") or 0) for r in batch) + 1
                if nxt <= cursor or nxt >= now() * 1000:
                    break
                cursor = nxt
            rates = {r["ts"]: r for r in rates if r["ts"] <= now()}
            return sorted(rates.values(), key=lambda r: r["ts"])

    def book(self, v, a, kind="perp"):
        with self.client_locks[v]:
            ex = self.client(v, kind == "spot" and v in SPOT_ADAPTERS)
            ex.neutralbasis_live = True
            with self.lock:
                row = next(
                    (
                        r
                        for r in self.rows.get(v, [])
                        if r["asset"] == a and r["kind"] == kind
                    ),
                    None,
                )
            if not row:
                raise ValueError("Güncel piyasa kimliği yok")
            book = ex.fetch_order_book(row["symbol"], 100)
            at = now()
            t = finite(book.get("timestamp"))
            if t and abs(at - t / 1000) > 30:
                raise ValueError("Defter eski")
            multiplier = (
                number(ex.market(row["symbol"]).get("contractSize") or 1)
                if kind == "perp"
                else 1
            )
            bids = [
                (number(x[0]), number(x[1]) * multiplier)
                for x in book["bids"]
                if x[0] > 0 and x[1] > 0
            ]
            asks = [
                (number(x[0]), number(x[1]) * multiplier)
                for x in book["asks"]
                if x[0] > 0 and x[1] > 0
            ]
            if (
                not bids
                or not asks
                or max(x[0] for x in bids) >= min(x[0] for x in asks)
            ):
                raise ValueError("Boş/çapraz defter")
            return dict(
                bids=bids, asks=asks, received=at, audit_id=ex.neutralbasis_audit
            )


def expanded_view(engine, base):
    from desk_analytics import history_summary

    rows = []
    for r in base["markets"]:
        if now() - r["received"] <= 180:
            rows.append(
                dict(
                    r,
                    kind="perp",
                    price_type="mark",
                    quote="USDG" if r["venue"] == "Lighter" else "USD",
                    oi=None,
                    volume=None,
                    interval=3600,
                    execution=r["venue"] in ("Lighter", "Extended"),
                )
            )
    extra, status = engine.wide.snapshot()
    rows += extra
    pairs = []
    with engine.wide.lock:
        histories = dict(engine.wide.histories)
    for row in rows:
        v, a, k = row["venue"], row["asset"], row["kind"]
        cached = histories.get((v, a, k))
        rates = (
            cached["rates"]
            if cached
            else engine.feed.funding.get((v, a), []) if k == "perp" else []
        )
        for days, field in ((1, "paid24"), (7, "paid7")):
            if k == "spot":
                row[field] = 0.0
                continue
            interval = row.get("interval")
            samples = [r for r in rates if now() - days * 86400 < r["ts"] <= now()]
            row[field] = (
                sum(r["rate"] for r in samples)
                if interval and len(samples) >= int(days * 86400 / interval)
                else None
            )
    for a in engine.cfg["symbols"]:
        ps = [r for r in rows if r["asset"] == a and r["kind"] == "perp"]
        spots = [r for r in rows if r["asset"] == a and r["kind"] == "spot"]
        for first, second in list(itertools.combinations(ps, 2)) + [
            (s, p) for s in spots for p in ps
        ]:
            if not comparable_prices(first, second, now()):
                continue
            if first["kind"] == "spot":
                l, s = first, second
                pair_type = "spot-perp"
            else:
                l, s = sorted((first, second), key=lambda r: r["mark"])
                pair_type = "perp-perp"
            native = (
                next(
                    (
                        p
                        for p in base["pairs"]
                        if p["asset"] == a
                        and {p["long"], p["short"]} == {l["venue"], s["venue"]}
                    ),
                    None,
                )
                if pair_type == "perp-perp"
                else None
            )
            spread = math.log(s["mark"] / l["mark"])
            fee = (
                2 * (l["fee"] + s["fee"])
                if l.get("fee") is not None and s.get("fee") is not None
                else None
            )
            history = native.get("history") if native else None
            lh = histories.get((l["venue"], a, l["kind"]))
            sh = histories.get((s["venue"], a, s["kind"]))
            if lh and sh:
                history = history_summary(lh["prices"], sh["prices"], now())
            pairs.append(
                dict(
                    asset=a,
                    long=l["venue"],
                    short=s["venue"],
                    long_kind=l["kind"],
                    short_kind=s["kind"],
                    pair_type=pair_type,
                    long_price=l["mark"],
                    short_price=s["mark"],
                    long_price_type=l["price_type"],
                    short_price_type=s["price_type"],
                    spread=spread,
                    funding=(
                        None
                        if l["rate"] is None or s["rate"] is None
                        else s["rate"] - l["rate"]
                    ),
                    fee_roundtrip=fee,
                    history=history,
                    paid24=(
                        s["paid24"] - l["paid24"]
                        if l["paid24"] is not None and s["paid24"] is not None
                        else None
                    ),
                    paid7=(
                        s["paid7"] - l["paid7"]
                        if l["paid7"] is not None and s["paid7"] is not None
                        else None
                    ),
                    deep=bool(native and native.get("deep")),
                    eligible=bool(native and native.get("eligible")),
                    reason=(
                        native["reason"]
                        if native
                        else "Geniş tarama; paper yürütme onayı yok"
                    ),
                    min_volume=(
                        min(l["volume"], s["volume"])
                        if l.get("volume") is not None and s.get("volume") is not None
                        else None
                    ),
                    min_oi=(
                        min(l["oi"], s["oi"])
                        if l.get("oi") is not None and s.get("oi") is not None
                        else None
                    ),
                    leverage=(
                        min(l["leverage"], s["leverage"])
                        if l.get("leverage") and s.get("leverage")
                        else None
                    ),
                    long_interval=l["interval"],
                    short_interval=s["interval"],
                    received=min(l["received"], s["received"]),
                    source="native" if native else "CCXT public APIs",
                )
            )
    registry = []
    for v, spec in CATALOG.items():
        st = engine.feed.status.get(v, {}) if spec["native"] else status.get(v, {})
        registry.append(
            {
                **spec,
                **st,
                "status": st.get(
                    "status",
                    (
                        "ready"
                        if st.get("ok")
                        else "unverified" if spec["scan"] else "adapter_missing"
                    ),
                ),
                "fresh": any(r["venue"] == v for r in rows),
            }
        )
    return dict(
        markets=rows,
        pairs=pairs,
        registry=registry,
        coverage=dict(
            total=len(registry),
            adapters=sum(x["scan"] for x in registry),
            available=sum(x["fresh"] for x in registry),
            cex=sum(x["fresh"] and x["kind"] == "CEX" for x in registry),
            dex=sum(x["fresh"] and x["kind"] == "DEX" for x in registry),
            version=getattr(ccxt, "__version__", None) if ccxt else None,
        ),
    )
