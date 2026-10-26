"""Five-venue selection and pairwise historical risk. Lighter = Robinhood instance only."""

import concurrent.futures, itertools, json, math, statistics, time, urllib.parse, urllib.request, email.utils
from decimal import Decimal, ROUND_DOWN
import bot
from bot import Feed, Engine, number, now, hour, analyze, walk
from connections import CATALOG

VENUES = ("TxFlow", "RISEx", "Arcus", "Extended", "Lighter")
DISPLAY = {
    "Lighter": "Lighter RH",
    "TxFlow": "TxFlow",
    "RISEx": "RISEx",
    "Arcus": "Arcus",
    "Extended": "Extended",
}
RH = "https://api.rh.lighter.xyz/api/v1/"
CATALOG.pop("Variational", None)
CATALOG["Lighter"].update(
    url=RH + "orderBookDetails",
    docs="https://apidocs.rh.lighter.xyz/docs/get-started",
    display="Lighter RH",
    note="Robinhood Chain bağımsız Lighter instance. Lighter Core verisi kullanılmaz; teminat USDG.",
)
for v in VENUES:
    CATALOG[v]["risk"] = True
    if v not in ("Lighter", "Extended"):
        CATALOG[v][
            "note"
        ] = "Piyasa ve fiyat uyumu risk taramasında. Paper yürütme / funding ödeme adaptörü henüz onaylı değil."


def epoch(x):
    # Thresholds are explicit; never interpret nanoseconds as milliseconds.
    n = Decimal(str(x))
    if not n.is_finite():
        raise ValueError("Geçersiz zaman damgası")
    if n >= 10**17:
        return float(n / 10**9)
    if n >= 10**14:
        return float(n / 10**6)
    if n >= 10**11:
        return float(n / 10**3)
    return float(n)


def shortlist(meta, symbols, limit=5):
    rows = []
    for asset in symbols:
        available = [
            v
            for v in VENUES
            if asset in meta.get(v, {}) and meta[v][asset].get("rate") is not None
        ]
        for a, b in itertools.combinations(available, 2):
            lv, sv = (
                (a, b) if meta[a][asset]["rate"] <= meta[b][asset]["rate"] else (b, a)
            )
            lm, sm = meta[lv][asset], meta[sv][asset]
            gap = sm["rate"] - lm["rate"]
            if gap <= 0:
                continue
            spread = (sm["mark"] - lm["mark"]) / ((sm["mark"] + lm["mark"]) / 2)
            rows.append(
                dict(
                    asset=asset,
                    long=lv,
                    short=sv,
                    funding_gap=gap,
                    price_spread=spread,
                    price_abs=abs(spread),
                    rate_long=lm["rate"],
                    rate_short=sm["rate"],
                    next_long=lm["next"],
                    next_short=sm["next"],
                    rate_basis_long=lm.get("rate_basis"),
                    rate_basis_short=sm.get("rate_basis"),
                    source_ids=[lm["audit_id"], sm["audit_id"]],
                )
            )
    # Rank fusion prevents unlike units from being added. An adverse entry spread is not a benefit.
    funding = sorted(
        rows, key=lambda c: (-c["funding_gap"], c["asset"], c["long"], c["short"])
    )
    prices = sorted(
        rows,
        key=lambda c: (
            -max(0, c["price_spread"]),
            -c["funding_gap"],
            c["asset"],
            c["long"],
            c["short"],
        ),
    )
    for i, c in enumerate(funding):
        c["funding_rank"] = i + 1
    for i, c in enumerate(prices):
        c["spread_rank"] = i + 1
        c["selection_score"] = (c["funding_rank"] + c["spread_rank"]) / 2
    result = sorted(
        rows,
        key=lambda c: (
            c["selection_score"],
            -c["funding_gap"],
            c["asset"],
            c["long"],
            c["short"],
        ),
    )[:limit]
    for i, c in enumerate(result):
        c["shortlist_rank"] = i + 1
    return result, len(rows)


class FiveFeed(Feed):
    def __init__(self, store):
        super().__init__(store)
        self.tx_meta = {}
        self.arcus_fee = None
        bot.L = RH

    def req(self, url, data=None, max_age=None):
        if data is None:
            return super().get(url, max_age)
        begin = now()
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode(),
            headers={
                "User-Agent": "FundingDeskRisk/0.5",
                "Content-Type": "application/json",
                "x-api-version": "2026-08-16",
                "Cache-Control": "no-cache",
            },
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read(15_000_000)
            headers = {k.lower(): v for k, v in r.headers.items()}
        received = now()
        aid = self.store.audit(url, raw, received, headers)
        o = json.loads(raw)
        if not o.get("success"):
            raise ValueError("TxFlow public sorgusu başarısız")
        age = number(headers.get("age", 0))
        if headers.get("date"):
            age = max(
                age,
                received
                - email.utils.parsedate_to_datetime(headers["date"]).timestamp(),
            )
        if max_age is not None and (age > max_age or received - begin > 15):
            raise ValueError("Eski veya gecikmiş TxFlow snapshot")
        return o, dict(received=received, audit_id=aid, http_age=max(0, age))

    def generic(
        self,
        v,
        asset,
        identifier,
        mark,
        index,
        rate,
        interval,
        next_time,
        info,
        **extra,
    ):
        if interval <= 0 or mark <= 0 or index <= 0:
            raise ValueError("Geçersiz fiyat veya funding aralığı")
        return dict(
            venue=v,
            asset=asset,
            id=identifier,
            mark=mark,
            index=index,
            rate=rate / (interval / 3600),
            raw_rate=rate,
            interval=interval,
            next=next_time,
            rate_ts=info["received"],
            step=extra.pop("step", 0.000001),
            minimum=extra.pop("minimum", 0.000001),
            min_quote=10.0,
            max_leverage=extra.pop("max_leverage", 5.0),
            mm=0.05,
            rfq=False,
            **info,
            **extra,
        )

    def markets(self):
        # Core/Ethereum host is never contacted by this feed.
        super().markets()
        meta = self.meta
        for v in ("Lighter", "Extended"):
            for m in meta.get(v, {}).values():
                m.update(
                    interval=3600,
                    price_type="trade",
                    rate_basis=(
                        "Güncel saatlik tahmin"
                        if v == "Extended"
                        else "Son gerçekleşen saatlik oran"
                    ),
                    deployment="Robinhood Chain" if v == "Lighter" else "Extended",
                )

        def rh_rates():
            def one(a):
                try:
                    self.funding_history("Lighter", a)
                except Exception:
                    meta["Lighter"][a]["rate"] = None

            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as p:
                list(
                    p.map(
                        one, [a for a in self.symbols if a in meta.get("Lighter", {})]
                    )
                )

        def arcus():
            o, info = self.req("https://api.arcus.xyz/v1/markets", max_age=60)
            out = {}
            for d in o["markets"]:
                a = d["baseAsset"]
                if a not in self.symbols or d["status"] != "ONLINE":
                    continue
                m = self.generic(
                    "Arcus",
                    a,
                    d["marketDisplayName"],
                    number(d["markPrice"]),
                    number(d["oraclePrice"]),
                    number(d["fundingRate"]),
                    3600,
                    epoch(d["nextFundingAt"]),
                    info,
                    step=number(d["stepSize"]),
                    minimum=number(d["minOrderSize"]),
                    max_leverage=1 / number(d["initialMarginFraction"]),
                    price_type="oracle",
                    rate_basis="API saatlik funding; tahmin olarak kullanılır",
                )
                out[a] = m
            fees, fi = self.req("https://api.arcus.xyz/v1/feetiers")
            self.arcus_fee = max(
                number(t["taker_fee_ppm"]) / 1e6 for t in fees["tiers"]
            )
            return "Arcus", out

        def rise():
            o, info = self.req("https://api.rise.trade/v1/markets", max_age=60)
            out = {}
            for d in o["data"]["markets"]:
                a = d["underlying"].split("/")[0]
                c = d["config"]
                if (
                    a not in self.symbols
                    or not d.get("active")
                    or not c["unlocked"]
                    or d.get("reduce_only")
                    or d.get("category") != "crypto"
                ):
                    continue
                interval = number(d["funding_interval"]) / 1e9
                out[a] = self.generic(
                    "RISEx",
                    a,
                    d["market_id"],
                    number(d["mark_price"]),
                    number(d["index_price"]),
                    number(d["current_funding_rate"]),
                    interval,
                    epoch(d["next_funding_time"]),
                    info,
                    step=number(c["step_size"]),
                    minimum=number(c["min_order_size"]),
                    max_leverage=number(c["max_leverage"]),
                    price_type="trade",
                    rate_basis="API current_funding_rate / funding_interval",
                )
            return "RISEx", out

        def tx():
            o, info = self.req(
                "https://rpc.txflow.com/info",
                {"type": "perpMeta", "exchangeId": "all"},
                60,
            )
            universe = {
                d["baseCurrency"]: d
                for d in o["data"]["universe"]
                if d["baseCurrency"] in self.symbols
                and not d.get("haltTrading")
                and not d.get("delisted")
            }
            self.tx_meta = universe
            out = {}

            def one(item):
                a, d = item
                x, inf = self.req(
                    "https://rpc.txflow.com/info",
                    {"type": "activeAssetCtx", "coin": str(d["index"])},
                    60,
                )
                ctx = x["data"].get("nodeCtx", {})
                interval = epoch(ctx["nextFundingTime"]) - epoch(ctx["prevFundingTime"])
                if abs(now() - epoch(ctx["blockTime"])) > 90:
                    raise ValueError("TxFlow blok verisi güncel değil")
                return a, self.generic(
                    "TxFlow",
                    a,
                    str(d["index"]),
                    number(ctx["markPx"]),
                    number(ctx["oraclePx"]),
                    number(ctx["funding"]) / 100,
                    interval,
                    epoch(ctx["nextFundingTime"]),
                    inf,
                    step=number(d["basePrecision"]),
                    minimum=number(d["basePrecision"]),
                    max_leverage=number(d["maxLeverage"]),
                    price_type="trade",
                    rate_basis="nodeCtx funding yüzde / gerçek ödeme aralığı",
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as p:
                fs = [p.submit(one, item) for item in universe.items()]
                for f in fs:
                    try:
                        a, m = f.result()
                        out[a] = m
                    except Exception:
                        continue
            return "TxFlow", out

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as p:
            fs = {
                p.submit(fn): v
                for fn, v in ((arcus, "Arcus"), (rise, "RISEx"), (tx, "TxFlow"))
            }
            r = p.submit(rh_rates)
            for f, v in fs.items():
                try:
                    v, out = f.result()
                    meta[v] = out
                    self.status[v] = dict(
                        ok=bool(out),
                        ts=now(),
                        message="Piyasa + normalize funding risk taramasında; "
                        + str(len(out))
                        + " seçili varlık",
                    )
                except Exception as ex:
                    self.status[v] = dict(ok=False, ts=now(), message=str(ex))
            r.result()
        self.status.pop("Variational", None)
        self.meta = meta
        for v in meta:
            for a, m in meta[v].items():
                self.store.mark(
                    v, a, m["received"], m["index"] if v == "Lighter" else m["mark"]
                )
        return meta

    def candles(self, v, asset):
        if v in ("Lighter", "Extended"):
            o = {**super().candles(v, asset), "price_type": "trade"}
            self.history[(v, asset)] = o
            return o
        key = (v, asset, hour(now()))
        if key in self.cache:
            return self.cache[key]
        m = self.meta[v][asset]
        end = hour(now())
        start = end - 721 * 3600
        if v == "TxFlow":
            o, info = self.req(
                "https://rpc.txflow.com/info",
                {
                    "type": "candleSnapshot",
                    "req": {
                        "coin": str(m["id"]),
                        "interval": "1h",
                        "startTime": start * 1000,
                        "endTime": end * 1000,
                    },
                },
            )
            rows = {int(epoch(x["t"])): number(x["c"]) for x in o["data"]}
        elif v == "RISEx":
            o, info = self.req(
                "https://api.rise.trade/v1/markets/id/"
                + m["id"]
                + "/trading-view-data?"
                + urllib.parse.urlencode(
                    {"interval": 3600 * 10**9, "from": start * 10**9, "to": end * 10**9}
                )
            )
            rows = {
                int(epoch(x["time"])): number(x["close"]) for x in o["data"]["data"]
            }
        else:
            o, info = self.req(
                "https://api.arcus.xyz/v1/candles?"
                + urllib.parse.urlencode(
                    dict(
                        market=m["id"],
                        timeframe="1h",
                        **{"from": start * 10**6, "to": end * 10**6},
                    )
                )
            )
            rows = {
                int(epoch(x["openTime"])): number(x["close"])
                for x in o["candles"]
                if x.get("isFinal", True)
            }
        rows = {t: p for t, p in rows.items() if start <= t < end and p > 0}
        out = dict(
            prices=rows,
            audit_ids=[info["audit_id"]],
            end=end,
            price_type=m["price_type"],
        )
        self.cache[key] = out
        self.history[(v, asset)] = out
        return out

    WINDOW = 15 * 86400

    def windows(self, start, end):
        """Back-to-back windows small enough for every venue's per-request row limit."""
        t = end
        while t > start:
            yield max(start, t - self.WINDOW), t
            t = max(start, t - self.WINDOW)

    def candles_range(self, v, asset, start, end):
        """Closed 1h trade/oracle candles over an arbitrary range (backtests up to 90 days)."""
        m = self.meta[v][asset]
        prices, ids = {}, []
        for s, e in self.windows(start, end):
            if v == "Lighter":
                o, info = self.req(
                    bot.L
                    + "candles?"
                    + urllib.parse.urlencode(
                        dict(
                            market_id=m["id"],
                            resolution="1h",
                            start_timestamp=s,
                            end_timestamp=e,
                            count_back=500,
                        )
                    )
                )
                rows = {
                    int(bot.stamp(x["t"])): number(x["c"]) for x in o["c"] if x.get("c")
                }
            elif v == "Extended":
                o, info = self.req(
                    bot.E
                    + "info/candles/"
                    + m["id"]
                    + "/trades?"
                    + urllib.parse.urlencode(
                        dict(interval="PT1H", limit=750, endTime=e * 1000)
                    )
                )
                rows = {int(bot.stamp(x["T"])): number(x["c"]) for x in o["data"]}
            elif v == "TxFlow":
                o, info = self.req(
                    "https://rpc.txflow.com/info",
                    {
                        "type": "candleSnapshot",
                        "req": {
                            "coin": str(m["id"]),
                            "interval": "1h",
                            "startTime": s * 1000,
                            "endTime": e * 1000,
                        },
                    },
                )
                rows = {int(epoch(x["t"])): number(x["c"]) for x in o["data"]}
            elif v == "RISEx":
                o, info = self.req(
                    "https://api.rise.trade/v1/markets/id/"
                    + m["id"]
                    + "/trading-view-data?"
                    + urllib.parse.urlencode(
                        {"interval": 3600 * 10**9, "from": s * 10**9, "to": e * 10**9}
                    )
                )
                rows = {
                    int(epoch(x["time"])): number(x["close"]) for x in o["data"]["data"]
                }
            else:
                o, info = self.req(
                    "https://api.arcus.xyz/v1/candles?"
                    + urllib.parse.urlencode(
                        dict(
                            market=m["id"],
                            timeframe="1h",
                            **{"from": s * 10**6, "to": e * 10**6},
                        )
                    )
                )
                rows = {
                    int(epoch(x["openTime"])): number(x["close"])
                    for x in o["candles"]
                    if x.get("isFinal", True)
                }
            ids.append(info["audit_id"])
            prices.update({t: p for t, p in rows.items() if s <= t < e and p > 0})
        return dict(
            prices=prices,
            audit_ids=ids,
            end=end,
            price_type=(
                "trade"
                if v in ("Lighter", "Extended")
                else m.get("price_type", "trade")
            ),
        )

    def funding_range(self, v, asset, start, end):
        """Realized funding over an arbitrary range, paged per window; never invents a rate."""
        m = self.meta[v][asset]
        if v == "TxFlow":
            raise ValueError(
                "TxFlow public gerçekleşmiş funding geçmişi / pozisyon bazında tahakkuk modeli doğrulanmadı"
            )
        out = {}
        for s, e in self.windows(start, end):
            if v == "Lighter":
                o, info = self.req(
                    bot.L
                    + "fundings?"
                    + urllib.parse.urlencode(
                        dict(
                            market_id=m["id"],
                            resolution="1h",
                            start_timestamp=int(s),
                            end_timestamp=int(e),
                            count_back=750,
                        )
                    )
                )
                rows = []
                for x in o["fundings"]:
                    sign = {"long": 1, "short": -1}.get(x["direction"])
                    if sign is None:
                        raise ValueError("Bilinmeyen funding yönü")
                    rows.append(
                        dict(
                            ts=bot.stamp(x["timestamp"]),
                            rate=abs(number(x["rate"])) / 100 * sign,
                            value=abs(number(x["value"])) * sign,
                        )
                    )
            elif v == "Extended":
                o, info = self.req(
                    bot.E
                    + "info/"
                    + m["id"]
                    + "/funding?"
                    + urllib.parse.urlencode(
                        dict(startTime=int(s * 1000), endTime=int(e * 1000), limit=1000)
                    )
                )
                rows = [
                    dict(ts=bot.stamp(x["T"]), rate=number(x["f"]), value=None)
                    for x in o["data"]
                ]
            elif v == "RISEx":
                o, info = self.req(
                    "https://api.rise.trade/v1/markets/id/"
                    + m["id"]
                    + "/funding-rate-history?"
                    + urllib.parse.urlencode(
                        dict(start_time=int(s * 1e9), end_time=int(e * 1e9), limit=1000)
                    )
                )
                rows = [
                    dict(
                        ts=epoch(x["end_time"]),
                        rate=number(x["funding_rate"]),
                        value=number(x["index_price"]) * number(x["funding_rate"]),
                    )
                    for x in o["data"]["records"]
                ]
            else:
                o, info = self.req(
                    "https://api.arcus.xyz/v1/fundingRates?"
                    + urllib.parse.urlencode(
                        dict(
                            market=m["id"],
                            limit=1000,
                            **{"from": int(s * 1e6), "to": int(e * 1e6)},
                        )
                    )
                )
                rows = [
                    dict(ts=epoch(x["time"]), rate=number(x["fundingRate"]), value=None)
                    for x in o["fundingRates"]
                ]
            for r in rows:
                if s <= r["ts"] <= e:
                    out[r["ts"]] = dict(r, audit_id=info["audit_id"])
        return sorted(out.values(), key=lambda r: r["ts"])

    def funding_history(self, v, asset, start=None):
        if v in ("Lighter", "Extended"):
            return super().funding_history(v, asset, start)
        start = start if start is not None else now() - 7 * 86400
        m = self.meta[v][asset]
        if v == "TxFlow":
            raise ValueError(
                "TxFlow public gerçekleşmiş funding geçmişi / pozisyon bazında tahakkuk modeli doğrulanmadı"
            )
        if v == "RISEx":
            o, info = self.req(
                "https://api.rise.trade/v1/markets/id/"
                + m["id"]
                + "/funding-rate-history?"
                + urllib.parse.urlencode(
                    dict(
                        start_time=int(start * 1e9),
                        end_time=int(now() * 1e9),
                        limit=1000,
                    )
                )
            )
            rows = [
                dict(
                    ts=epoch(x["end_time"]),
                    rate=number(x["funding_rate"]),
                    value=number(x["index_price"]) * number(x["funding_rate"]),
                    audit_id=info["audit_id"],
                )
                for x in o["data"]["records"]
            ]
        else:
            o, info = self.req(
                "https://api.arcus.xyz/v1/fundingRates?"
                + urllib.parse.urlencode(
                    dict(
                        market=m["id"],
                        limit=1000,
                        **{"from": int(start * 1e6), "to": int(now() * 1e6)},
                    )
                )
            )
            rows = [
                dict(
                    ts=epoch(x["time"]),
                    rate=number(x["fundingRate"]),
                    value=None,
                    audit_id=info["audit_id"],
                )
                for x in o["fundingRates"]
            ]
        rows = sorted(
            [r for r in rows if start <= r["ts"] <= now()], key=lambda r: r["ts"]
        )
        if not rows or now() - rows[-1]["ts"] > max(4000, m["interval"] + 300):
            raise ValueError("Gerçekleşmiş funding geçmişi eksik/eski")
        self.funding[(v, asset)] = rows
        return rows

    def book(self, v, a):
        if v in ("Lighter", "Extended"):
            return super().book(v, a)
        m = self.meta[v][a]
        if v == "TxFlow":
            o, info = self.req(
                "https://rpc.txflow.com/info",
                {"type": "l2Book", "coin": str(m["id"])},
                30,
            )
            d = o["data"]
            b = [(number(x["px"]), number(x["sz"])) for x in d["levels"][0]]
            s = [(number(x["px"]), number(x["sz"])) for x in d["levels"][1]]
            source = epoch(d["time"])
        elif v == "RISEx":
            o, info = self.req(
                "https://api.rise.trade/v1/orderbook?market_id="
                + m["id"]
                + "&limit=250",
                max_age=30,
            )
            d = o["data"]
            b = [(number(x["price"]), number(x["quantity"])) for x in d["bids"]]
            s = [(number(x["price"]), number(x["quantity"])) for x in d["asks"]]
            source = info["received"]
        else:
            o, info = self.req(
                "https://api.arcus.xyz/v1/l2OrderBook/" + m["id"], max_age=30
            )
            b = [(number(x[0]), number(x[1])) for x in o["bids"]]
            s = [(number(x[0]), number(x[1])) for x in o["asks"]]
            source = epoch(o["timestamp"])
        b = [x for x in b if x[0] > 0 and x[1] > 0]
        s = [x for x in s if x[0] > 0 and x[1] > 0]
        if (
            not b
            or not s
            or max(x[0] for x in b) >= min(x[0] for x in s)
            or abs(now() - source) > 30
        ):
            raise ValueError("Emir defteri boş, çapraz veya eski")
        return dict(bids=b, asks=s, **info)


class RiskEngine(Engine):
    def __init__(self, path, feed=None):
        super().__init__(path, feed)
        self.feed = feed or FiveFeed(self.store)
        self.feed.symbols = self.cfg["symbols"]
        self.selected = []
        self.universe_count = 0
        if not self.store.get("wallet", None):
            self.wallet = {v: self.cfg["capital"] / 5 for v in VENUES}
        if set(self.wallet) != set(VENUES):
            self.store.db.close()
            raise ValueError(
                "Beş DEX / Robinhood ayrı veri klasörü gerekli; eski bakiyeler karıştırılamaz"
            )
        self.stage = "Bekleniyor"

    def fee(self, v):
        if v == "Lighter":
            return self.cfg["lighter_fee"]
        if v == "Extended":
            return self.cfg["extended_fee"]
        if v == "Arcus" and self.feed.arcus_fee is not None:
            return self.feed.arcus_fee
        raise ValueError(v + " taker ücret tarifesi doğrulanmadı")

    def assess(self, c):
        a, lv, sv = c["asset"], c["long"], c["short"]
        out = {
            **c,
            "eligible": False,
            "risk_eligible": False,
            "reasons": [],
            "net": None,
        }
        try:
            hs = {v: self.feed.candles(v, a) for v in (lv, sv)}
            ana = analyze(hs[lv], hs[sv], self.cfg["horizon"], now())
            out["analysis"] = ana
            times = sorted(set(hs[lv]["prices"]) & set(hs[sv]["prices"]))[-168:]
            base_l = hs[lv]["prices"][times[0]]
            base_s = hs[sv]["prices"][times[0]]
            out["history"] = [
                dict(
                    ts=t + 3600,
                    long=hs[lv]["prices"][t],
                    short=hs[sv]["prices"][t],
                    long_index=hs[lv]["prices"][t] / base_l * 100,
                    short_index=hs[sv]["prices"][t] / base_s * 100,
                    spread=(hs[lv]["prices"][t] - hs[sv]["prices"][t])
                    / ((hs[lv]["prices"][t] + hs[sv]["prices"][t]) / 2),
                )
                for t in times
            ]
            out["price_types"] = {v: hs[v]["price_type"] for v in (lv, sv)}
            lm, sm = self.feed.meta[lv][a], self.feed.meta[sv][a]
            reference_qty = self.cfg["size"] / max(lm["mark"], sm["mark"])
            reference_size = reference_qty * (lm["mark"] + sm["mark"]) / 2
            out["reference_notional"] = reference_size
            out["reference_funding"] = (
                reference_qty
                * (
                    (sm["index"] if sv == "Lighter" else sm["mark"]) * sm["rate"]
                    - (lm["index"] if lv == "Lighter" else lm["mark"]) * lm["rate"]
                )
                * self.cfg["horizon"]
            )
            out["reference_stress"] = reference_size * (
                ana["stress_fraction"] + self.cfg["collateral_stress_bps"] / 10000
            )
            out["reference_coverage"] = (
                out["reference_funding"] / out["reference_stress"]
                if out["reference_stress"] > 0
                else None
            )
            if out["reference_funding"] <= out["reference_stress"]:
                out["reasons"].append(
                    "Hedef büyüklükte funding göstergesi spread/teminat stresini karşılamıyor; ücretler hariç bile avantaj yetersiz"
                )
            if any(hs[v]["price_type"] != "trade" for v in (lv, sv)):
                out["reasons"].append(
                    "Oracle mumları gerçek perp işlem fiyatı uyumu değildir; Arcus fiyat/basis geçmişi doğrulanmalı"
                )
            for v in (lv, sv):
                self.feed.funding_history(v, a)
            bs = {v: self.feed.book(v, a) for v in (lv, sv)}
            early_reasons = list(out["reasons"])
            quote = self.evaluate(a, lv, sv, bs, ana)
            out.update(quote)
            out["reasons"] = early_reasons + out["reasons"]
            out.update(
                {
                    k: c[k]
                    for k in (
                        "shortlist_rank",
                        "selection_score",
                        "funding_rank",
                        "spread_rank",
                        "funding_gap",
                        "price_spread",
                        "price_abs",
                        "rate_basis_long",
                        "rate_basis_short",
                        "next_long",
                        "next_short",
                    )
                }
            )
            if any(hs[v]["price_type"] != "trade" for v in (lv, sv)):
                out["reasons"].append("Oracle proxy geçmişi ile risk onayı verilmez")
            out["risk_eligible"] = not out["reasons"]
            if lv not in ("Lighter", "Extended") or sv not in ("Lighter", "Extended"):
                out["reasons"].append(
                    "Bu çift için paper ödeme/yürütme adaptörü henüz onaylı değil"
                )
            out["eligible"] = not out["reasons"]
        except Exception as ex:
            out["reasons"].append(str(ex))
        ana = out.get("analysis")
        if ana:
            if min(ana["corr24"], ana["corr7"]) < self.cfg["min_corr"]:
                out["reasons"].append("Getiri korelasyonu düşük")
            if not 0.8 <= ana["beta"] <= 1.2:
                out["reasons"].append("Hedge beta uyumsuz")
            if abs(ana["z"]) > self.cfg["max_z"]:
                out["reasons"].append("Spread z-score sınır dışında")
        out["reasons"] = list(dict.fromkeys(out["reasons"]))
        out["eligible"] = not out["reasons"]
        out["risk_eligible"] = bool(ana) and not out["reasons"]
        self.store.event("TOP5_RISK", out)
        return out

    def cycle(self):
        if self.busy:
            return
        self.busy = True
        try:
            self.error = None
            self.stage = "5 DEX piyasa ve funding taraması"
            self.feed.symbols = self.cfg["symbols"]
            self.feed.markets()
            self.selected, self.universe_count = shortlist(
                self.feed.meta, self.cfg["symbols"]
            )
            self.candidates = []
            self.stage = "İlk 5 çiftin eşlenmiş fiyat geçmişi ve risk analizi"
            for c in self.selected:
                self.candidates.append(self.assess(c))
            if hasattr(self, "extra_candidates"):
                extra = self.extra_candidates()
                keys = {(c["asset"], c["long"], c["short"]) for c in extra}
                self.candidates = [
                    c
                    for c in self.candidates
                    if (c["asset"], c["long"], c["short"]) not in keys
                ] + extra
            # Existing paper trades are always monitored, even if no longer among top five.
            for p in list(self.positions):
                a = p["asset"]
                vs = [x["venue"] for x in p["legs"] if not x["closed"]]
                try:
                    # A funding history outage must not stop marking or closing the legs.
                    for v in {x["venue"] for x in p["legs"]}:
                        try:
                            self.feed.funding_history(
                                v,
                                a,
                                min((x["entered"] for x in p["legs"]), default=now()),
                            )
                        except Exception as ex:
                            self.events(
                                "FUNDING_WAIT",
                                "Funding geçmişi alınamadı; fiyat izlemesi sürüyor",
                                position=p["id"],
                                venue=v,
                                error=str(ex),
                            )
                    self.settle(p)
                    books = {v: self.feed.book(v, a) for v in vs}
                    self.mark_position(p, books)
                    reason = None
                    if p["id"] in self.manual_close:
                        reason = "Kullanıcı paper kapatma talebi"
                    elif (
                        p["status"] in ("RECOVERY", "CLOSING", "OPENING")
                        or len(p["legs"]) != 2
                        or any(l["closed"] for l in p["legs"])
                    ):
                        reason = p.get("close_reason") or "Bacak kurtarma"
                    elif p["net_if_closed"] <= -self.cfg["risk"]:
                        reason = "Pozisyon kayıp limiti"
                    elif self.daily_pnl() <= -self.cfg["daily_loss"]:
                        reason = "Günlük kayıp limiti"
                    elif (
                        p["price_pnl_open"] + p["decision"]["execution_drag"]
                        < -p["decision"]["stress_loss"]
                    ):
                        reason = "Spread stres bütçesi aşıldı"
                    elif p["decision"].get("strategy") == "convergence":
                        from convergence import exit_reason

                        reason = exit_reason(p, books, self.cfg, now())
                    else:
                        c = next(
                            (
                                c
                                for c in self.candidates
                                if (c["asset"], c["long"], c["short"])
                                == (a, p["decision"]["long"], p["decision"]["short"])
                            ),
                            None,
                        )
                        if c is None:
                            c = self.assess(
                                dict(
                                    asset=a,
                                    long=p["decision"]["long"],
                                    short=p["decision"]["short"],
                                    **{
                                        k: p["decision"].get(k)
                                        for k in (
                                            "shortlist_rank",
                                            "selection_score",
                                            "funding_rank",
                                            "spread_rank",
                                            "funding_gap",
                                            "price_spread",
                                            "price_abs",
                                            "rate_basis_long",
                                            "rate_basis_short",
                                            "next_long",
                                            "next_short",
                                        )
                                    },
                                )
                            )
                        ana = c.get("analysis")
                        if (
                            not ana
                            or min(ana["corr24"], ana["corr7"]) < self.cfg["min_corr"]
                            or not 0.8 <= ana["beta"] <= 1.2
                        ):
                            reason = "Fiyat geçmişi / hedge uyumu bozuldu"
                        elif abs(ana["z"]) > self.cfg["max_z"]:
                            reason = "Spread z-score limiti"
                        lm = self.feed.meta[p["decision"]["long"]][a]
                        sm = self.feed.meta[p["decision"]["short"]][a]
                        remaining = (
                            p["decision"]["qty"]
                            * (sm["mark"] * sm["rate"] - lm["mark"] * lm["rate"])
                            * self.cfg["horizon"]
                            - p["decision"]["stress_loss"]
                            - sum(
                                l["qty"]
                                * l.get("exit_estimate", l["entry"])
                                * l["fee_rate"]
                                for l in p["legs"]
                                if not l["closed"]
                            )
                        )
                        p["remaining_net_estimate"] = remaining
                        if remaining < self.cfg["min_net"]:
                            reason = (
                                reason
                                or "Kalan funding avantajı çıkış masrafı ve stresi karşılamıyor"
                            )
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
            for p in self.closed:
                if p.get("pending_funding"):
                    for l in p["legs"]:
                        try:
                            self.feed.funding_history(
                                l["venue"],
                                p["asset"],
                                min(x["entered"] for x in p["legs"]),
                            )
                        except Exception:
                            pass
                    self.settle(p)
                    p["net"] = p["price_pnl"] + p["funding"] - p["fees"]
            eligible = sorted(
                [c for c in self.candidates if c["eligible"]],
                key=lambda c: c["net"],
                reverse=True,
            )
            if (
                self.running
                and eligible
                and not any(
                    p.get("pending_funding") for p in self.positions + self.closed
                )
                and len(self.positions) < self.cfg["max_positions"]
                and not any(p["status"] != "OPEN" for p in self.positions)
            ):
                c = eligible[0]
                bs = {v: self.feed.book(v, c["asset"]) for v in (c["long"], c["short"])}
                fresh = self.evaluate(
                    c["asset"], c["long"], c["short"], bs, c["analysis"]
                )
                if fresh["eligible"] and self.running:
                    self.open({**c, **fresh})
            self.last_cycle = now()
            self.stage = "Tamamlandı"
            self.persist()
        except Exception as ex:
            self.error = str(ex)
            self.events("ERROR", str(ex))
            self.persist()
        finally:
            self.busy = False

    def update_config(self, changes):
        # Validate symbols against any two requested venues, not just the execution-ready pair.
        symbols = changes.get("symbols")
        rest = {k: v for k, v in changes.items() if k != "symbols"}
        old = dict(self.cfg)
        if symbols is not None:
            if (
                not isinstance(symbols, list)
                or len(symbols) != 15
                or len(set(symbols)) != 15
                or any(
                    not isinstance(a, str) or not a.isalnum() or not a.isupper()
                    for a in symbols
                )
            ):
                raise ValueError("15 farklı varlık gerekli")
            if self.feed.meta and any(
                sum(a in m for m in self.feed.meta.values()) < 2 for a in symbols
            ):
                raise ValueError(
                    "Her varlık en az iki seçili DEX üzerinde listelenmeli"
                )
        super().update_config(rest)
        if symbols is not None:
            self.cfg["symbols"] = symbols
        if rest.get("capital") and old["capital"] != self.cfg["capital"]:
            self.wallet = {v: self.cfg["capital"] / 5 for v in VENUES}
        self.persist()

    def snapshot(self):
        o = super().snapshot()
        o.update(
            venues=list(VENUES),
            venue_labels=DISPLAY,
            selection=dict(
                limit=5,
                universe=self.universe_count,
                stage=self.stage,
                method="Pozitif normalize funding farkı + pozitif giriş mark spread sıralamalarının eşit ağırlıklı sıra ortalaması; ilk 5 farklı varlık/DEX çifti.",
            ),
            deployment="Lighter Robinhood Chain / USDG; Lighter Core kullanılmaz",
            valuation="Gerçek piyasa fiyatları ve resmî kaynak kayıtları. USD, USDC ve USDG 1:1 varsayımı + teminat stres payı; depeg için kesin koruma yok. Arcus oracle mumları gerçek perp fiyat uyumu sayılmaz. Paper emirler yalnızca onaylı Robinhood Lighter–Extended çiftinde.",
        )
        return o
