"""Local-only paper execution. No wallet, private key or exchange write endpoint."""

from __future__ import annotations
import argparse, base64, concurrent.futures, datetime as dt, email.utils, hashlib, hmac, json, math, os, signal
from pathlib import Path
import sqlite3, statistics, sys, threading, time, traceback, urllib.parse, urllib.request, uuid, zlib
from decimal import Decimal, ROUND_DOWN
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from connections import Connections

ROOT = Path(__file__).resolve().parent
DEFAULTS = dict(
    capital=1000.0,
    leverage=5.0,
    reserve=0.30,
    risk=5.0,
    daily_loss=20.0,
    size=1000.0,
    horizon=24,
    min_net=0.25,
    min_corr=0.95,
    max_z=2.5,
    poll_seconds=30,
    latency_ms=750,
    extra_slippage_bps=1.0,
    collateral_stress_bps=5.0,
    extended_fee=0.00025,
    lighter_fee=0.00028,
    max_positions=1,
    cooldown_seconds=3600,
    symbols=[
        "BTC",
        "ETH",
        "SOL",
        "HYPE",
        "SUI",
        "XRP",
        "LINK",
        "DOGE",
        "AVAX",
        "BNB",
        "LTC",
        "AAVE",
        "UNI",
        "NEAR",
        "ADA",
    ],
)
# A payment hour with no published rate after this wait is recorded as unverified
# zero cash so it no longer blocks new entries. A later published rate replaces it.
FUNDING_UNVERIFIED_AFTER = 3600
# Largest fill price move open() accepts versus the evaluated entry; sizing reserves for it.
FILL_TOLERANCE = 0.001
L = "https://mainnet.zklighter.elliot.ai/api/v1/"
E = "https://api.starknet.extended.exchange/api/v1/"


def now():
    return time.time()


def hour(t):
    return int(t) // 3600 * 3600


def number(v):
    n = float(v)
    if not math.isfinite(n):
        raise ValueError("Sonlu olmayan sayı")
    return n


def stamp(t):
    n = number(t)
    return n / 1000 if n > 1e11 else n


def corr(a, b):
    if len(a) < 2 or len(a) != len(b):
        raise ValueError("Korelasyon için yeterli eş veri yok")
    ma, mb = statistics.mean(a), statistics.mean(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        raise ValueError("Sabit fiyat serisi")
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return cov / math.sqrt(va * vb), cov / vb


def walk(levels, qty, buy, extra=0):
    if qty <= 0:
        raise ValueError("Pozitif miktar gerekli")
    remain = qty
    total = 0.0
    for p, q in sorted(levels, reverse=not buy):
        take = min(remain, q)
        total += take * p
        remain -= take
        if remain < 1e-10:
            break
    if remain > 1e-9:
        raise ValueError("Emir defteri derinliği yetersiz; kısmi dolum kabul edilmedi")
    return total / qty * (1 + extra if buy else 1 - extra)


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("pragma journal_mode=WAL")
        self.db.executescript(
            """create table if not exists kv(k text primary key,v text);
   create table if not exists audit(id integer primary key,ts real,url text,sha text,raw blob);
   create table if not exists events(id integer primary key,ts real,kind text,body text);
   create table if not exists marks(venue text,asset text,ts real,price real);
   create index if not exists marks_lookup on marks(venue,asset,ts);
   create table if not exists payments(key text primary key,ts real,body text);"""
        )
        self.db.commit()

    def get(self, k, default):
        with self.lock:
            r = self.db.execute("select v from kv where k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def put(self, k, v):
        with self.lock:
            self.db.execute("insert or replace into kv values(?,?)", (k, json.dumps(v)))
            self.db.commit()

    def event(self, kind, body):
        with self.lock:
            self.db.execute(
                "insert into events(ts,kind,body) values(?,?,?)",
                (now(), kind, json.dumps(body)),
            )
            self.db.commit()

    def audit(self, url, raw, received, headers):
        envelope = (
            json.dumps(dict(headers=headers, received=received)).encode() + b"\n" + raw
        )
        with self.lock:
            c = self.db.execute(
                "insert into audit(ts,url,sha,raw) values(?,?,?,?)",
                (
                    received,
                    url,
                    hashlib.sha256(envelope).hexdigest(),
                    zlib.compress(envelope),
                ),
            )
            self.db.commit()
            return c.lastrowid

    def mark(self, v, a, t, p):
        with self.lock:
            self.db.execute("insert into marks values(?,?,?,?)", (v, a, t, p))
            self.db.commit()

    def before(self, v, a, t):
        with self.lock:
            r = self.db.execute(
                "select ts,price from marks where venue=? and asset=? and ts<=? order by ts desc limit 1",
                (v, a, t),
            ).fetchone()
        return r if r and t - r[0] <= 90 else None

    def recent(self, table, limit=100):
        if table not in ("events", "payments"):
            raise ValueError("Geçersiz tablo")
        with self.lock:
            cols = [c[1] for c in self.db.execute("pragma table_info(" + table + ")")]
            rows = self.db.execute(
                "select * from " + table + " order by ts desc limit ?", (limit,)
            ).fetchall()
        return [{**dict(zip(cols, r)), "body": json.loads(r[-1])} for r in rows]


class Feed:
    def __init__(self, store):
        self.store = store
        self.meta = {}
        self.cache = {}
        self.status = {}
        self.history = {}
        self.funding = {}

    def get(self, url, max_age=None):
        begin = now()
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "FundingDeskPaper/0.3",
                "Accept": "application/json",
                "Cache-Control": "no-cache",
            },
        )
        with urllib.request.urlopen(request, timeout=12) as r:
            raw = r.read(20_000_000)
            headers = {k.lower(): v for k, v in r.headers.items()}
            received = now()
        aid = self.store.audit(url, raw, received, headers)
        age = number(headers.get("age", 0))
        date = headers.get("date")
        if date:
            age = max(
                age, received - email.utils.parsedate_to_datetime(date).timestamp()
            )
        if max_age is not None and (age > max_age or received - begin > 15):
            raise ValueError("Eski/yavaş HTTP verisi; yeni giriş engelli")
        obj = json.loads(raw)
        if obj.get("code", 200) != 200 or obj.get("status", "OK") != "OK":
            raise ValueError("Platform API hatası")
        return obj, dict(audit_id=aid, received=received, http_age=max(0, age))

    def markets(self):
        def fetch(v):
            url = L + "orderBookDetails" if v == "Lighter" else E + "info/markets"
            obj, info = self.get(url, 60)
            result = {}
            for d in obj["order_book_details"] if v == "Lighter" else obj["data"]:
                if v == "Lighter":
                    if (
                        d.get("status") != "active"
                        or d.get("market_type") != "perp"
                        or d.get("is_frozen")
                        or d.get("market_config", {}).get("force_reduce_only")
                    ):
                        continue
                    asset = d["symbol"]
                    m = dict(
                        venue=v,
                        asset=asset,
                        id=d["market_id"],
                        mark=number(d["mark_price"]),
                        index=number(d["index_price"]),
                        step=10 ** (-int(d["supported_size_decimals"])),
                        minimum=number(d["min_base_amount"]),
                        min_quote=number(d["min_quote_amount"]),
                        max_leverage=10000
                        / number(d["default_initial_margin_fraction"]),
                        mm=number(d["maintenance_margin_fraction"]) / 10000,
                        rfq=False,
                        rate=None,
                        rate_ts=None,
                        next=hour(now()) + 3600,
                    )
                else:
                    if (
                        not d.get("active")
                        or d.get("status") != "ACTIVE"
                        or d.get("type") != "PERPETUAL"
                        or d.get("category") != "Crypto"
                    ):
                        continue
                    asset = d["assetName"]
                    s = d["marketStats"]
                    c = d["tradingConfig"]
                    m = dict(
                        venue=v,
                        asset=asset,
                        id=d["name"],
                        mark=number(s["markPrice"]),
                        index=number(s["indexPrice"]),
                        step=number(c["minOrderSizeChange"]),
                        minimum=number(c["minOrderSize"]),
                        min_quote=0,
                        max_leverage=number(c["maxLeverage"]),
                        mm=0.025,
                        rfq=d.get("isRfq", False),
                        rate=number(s["fundingRate"]),
                        rate_ts=info["received"],
                        next=stamp(s["nextFundingRate"]),
                        max_order=number(c["maxMarketOrderValue"]),
                    )
                if m["mark"] <= 0 or m["index"] <= 0:
                    continue
                m.update(info)
                result[asset] = m
                self.store.mark(
                    v,
                    asset,
                    info["received"],
                    m["index"] if v == "Lighter" else m["mark"],
                )
            return v, result

        result = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fs = {pool.submit(fetch, v): v for v in ("Lighter", "Extended")}
            for f, v in fs.items():
                try:
                    key, value = f.result()
                    result[key] = value
                    self.status[key] = dict(
                        ok=True,
                        ts=now(),
                        message="Salt okunur piyasa verisi erişilebilir",
                    )
                except Exception as ex:
                    self.status[v] = dict(ok=False, ts=now(), message=str(ex))
        self.meta = result
        for v in ("TxFlow", "RISEx", "Arcus", "Variational"):
            self.status[v] = dict(
                ok=False,
                ts=None,
                message="Adaptör bu sürümde bağlı değil; işlem havuzu dışında",
            )
        return result

    def candles(self, v, asset):
        key = (v, asset, hour(now()))
        if key in self.cache:
            return self.cache[key]
        m = self.meta[v][asset]
        end = hour(now())
        start = end - 721 * 3600
        rows = []
        ids = []
        if v == "Lighter":
            for left, right in ((start, start + 360 * 3600), (start + 360 * 3600, end)):
                url = (
                    L
                    + "candles?"
                    + urllib.parse.urlencode(
                        dict(
                            market_id=m["id"],
                            resolution="1h",
                            start_timestamp=left,
                            end_timestamp=right,
                            count_back=500,
                        )
                    )
                )
                o, info = self.get(url)
                ids.append(info["audit_id"])
                rows += o["c"]
            parsed = {int(stamp(x["t"])): number(x["c"]) for x in rows if x.get("c")}
        else:
            url = (
                E
                + "info/candles/"
                + m["id"]
                + "/trades?"
                + urllib.parse.urlencode(
                    dict(interval="PT1H", limit=750, endTime=end * 1000)
                )
            )
            o, info = self.get(url)
            ids.append(info["audit_id"])
            parsed = {int(stamp(x["T"])): number(x["c"]) for x in o["data"]}
        parsed = {t: p for t, p in parsed.items() if start <= t < end and p > 0}
        result = dict(prices=parsed, audit_ids=ids, end=end)
        self.cache[key] = result
        self.history[(v, asset)] = result
        return result

    def funding_reference(self, asset, payment_time):
        """Last closed Extended trade candle before settlement; an explicit mark proxy."""
        candle_start = hour(payment_time) - 3600
        key = ("funding_reference", asset, candle_start)
        if key in self.cache:
            return self.cache[key]
        market = self.meta.get("Extended", {}).get(asset, {})
        instrument = market.get("id", asset + "-USD")
        url = (
            E
            + "info/candles/"
            + instrument
            + "/trades?"
            + urllib.parse.urlencode(
                dict(interval="PT1H", limit=2, endTime=hour(payment_time) * 1000)
            )
        )
        data, audit = self.get(url)
        matches = [row for row in data["data"] if stamp(row["T"]) == candle_start]
        if len(matches) != 1:
            raise ValueError("Ödeme öncesi kapanmış Extended işlem mumu bulunamadı")
        price = number(matches[0]["c"])
        if price <= 0:
            raise ValueError("Geçersiz geçmiş değerleme fiyatı")
        result = dict(
            price=price,
            ts=candle_start + 3600,
            method="historical_trade_close_proxy",
            audit_id=audit["audit_id"],
        )
        self.cache[key] = result
        return result

    def funding_history(self, v, asset, start=None):
        m = self.meta[v][asset]
        end = now()
        start = start if start is not None else end - 7 * 86400
        if v == "Lighter":
            url = (
                L
                + "fundings?"
                + urllib.parse.urlencode(
                    dict(
                        market_id=m["id"],
                        resolution="1h",
                        start_timestamp=int(start),
                        end_timestamp=int(end),
                        count_back=750,
                    )
                )
            )
            o, info = self.get(url)
            rows = []
            for x in o["fundings"]:
                sign = (
                    1
                    if x["direction"] == "long"
                    else -1 if x["direction"] == "short" else None
                )
                if sign is None:
                    raise ValueError("Bilinmeyen funding yönü")
                rows.append(
                    dict(
                        ts=stamp(x["timestamp"]),
                        rate=abs(number(x["rate"])) / 100 * sign,
                        value=abs(number(x["value"])) * sign,
                        audit_id=info["audit_id"],
                    )
                )
        else:
            url = (
                E
                + "info/"
                + m["id"]
                + "/funding?"
                + urllib.parse.urlencode(
                    dict(
                        startTime=int(start * 1000), endTime=int(end * 1000), limit=1000
                    )
                )
            )
            o, info = self.get(url)
            rows = [
                dict(
                    ts=stamp(x["T"]),
                    rate=number(x["f"]),
                    value=None,
                    audit_id=info["audit_id"],
                )
                for x in o["data"]
            ]
        rows = sorted(
            [r for r in rows if start - 1 <= r["ts"] <= end], key=lambda r: r["ts"]
        )
        if not rows or end - rows[-1]["ts"] > 4000:
            raise ValueError("Son gerçekleşen funding verisi eksik/eski")
        self.funding[(v, asset)] = rows
        # Lighter quote feed has ambiguous horizon; use confirmed last hourly settlement as forecast.
        if v == "Lighter":
            m["rate"] = rows[-1]["rate"]
            m["rate_ts"] = rows[-1]["ts"]
        return rows

    def book(self, v, asset):
        m = self.meta[v][asset]
        if m["rfq"]:
            raise ValueError("RFQ gösterge defteri gerçek likidite sayılmaz")
        url = (
            L + f'orderBookOrders?market_id={m["id"]}&limit=250'
            if v == "Lighter"
            else E + "info/markets/" + m["id"] + "/orderbook"
        )
        o, info = self.get(url, 30)
        o = o if v == "Lighter" else o["data"]
        qkey = "remaining_base_amount" if v == "Lighter" else "qty"

        def levels(key):
            return [
                (number(x["price"]), number(x[qkey]))
                for x in o[key]
                if number(x[qkey]) > 0 and number(x["price"]) > 0
            ]

        b = levels("bids" if v == "Lighter" else "bid")
        a = levels("asks" if v == "Lighter" else "ask")
        if not b or not a or max(p for p, q in b) >= min(p for p, q in a):
            raise ValueError("Boş veya çapraz emir defteri")
        return dict(bids=b, asks=a, **info)


def analyze(long, short, horizon, at):
    lp, sp = long["prices"], short["prices"]
    end = hour(at)
    times = list(range(end - 720 * 3600, end, 3600))
    if any(t not in lp or t not in sp for t in times):
        raise ValueError("30 günlük kesintisiz ve eşlenmiş 1h mumlar tamamlanmadı")
    a = [lp[t] for t in times]
    b = [sp[t] for t in times]
    ra = [math.log(a[i] / a[i - 1]) for i in range(1, len(a))]
    rb = [math.log(b[i] / b[i - 1]) for i in range(1, len(b))]
    c24, _ = corr(ra[-24:], rb[-24:])
    c7, beta = corr(ra[-168:], rb[-168:])
    spread = [math.log(x / y) for x, y in zip(a[-168:], b[-168:])]
    sd = statistics.pstdev(spread)
    z = (spread[-1] - statistics.mean(spread)) / sd if sd > 1e-10 else 0
    h = int(horizon)
    losses = []
    for i in range(h, len(spread)):
        # Holding equal base quantity: short outperforming long is adverse.
        losses.append(
            max(0, (b[-168 + i] - b[-168 + i - h]) - (a[-168 + i] - a[-168 + i - h]))
            / ((a[-168 + i - h] + b[-168 + i - h]) / 2)
        )
    adverse = max(losses, default=0)
    residual = statistics.pstdev([x - y for x, y in zip(ra[-168:], rb[-168:])])
    stress = max(adverse, 3 * residual * math.sqrt(h))
    return dict(
        corr24=c24,
        corr7=c7,
        beta=beta,
        z=z,
        stress_fraction=stress,
        volatility=residual,
        candles=720,
        audit_ids=long["audit_ids"] + short["audit_ids"],
    )


class Engine:
    def __init__(self, path, feed=None):
        self.store = Store(path)
        self.lock = threading.RLock()
        self.cfg = {**DEFAULTS, **self.store.get("config", {})}
        self.feed = feed or Feed(self.store)
        if self.cfg["symbols"] == DEFAULTS["symbols"][:10]:
            self.cfg["symbols"] = list(DEFAULTS["symbols"])
        self.connections = Connections(self.store)
        self.positions = self.store.get("positions", [])
        self.closed = self.store.get("closed", [])
        self.wallet = self.store.get(
            "wallet",
            {"Lighter": self.cfg["capital"] / 2, "Extended": self.cfg["capital"] / 2},
        )
        self.running = False
        self.stop_event = threading.Event()
        self.last_cycle = None
        self.candidates = []
        self.error = None
        self.manual_close = set()
        self.busy = False
        self.public_snapshot = None
        self.store.event(
            "BOOT",
            {
                "message": "Paper only; başlangıçta duraklatıldı. Açık pozisyonlar izlenir."
            },
        )

    def persist(self):
        # One transaction: prevent wallet/position divergence after interruption.
        with self.store.lock:
            for k, v in [
                ("positions", self.positions),
                ("closed", self.closed[-500:]),
                ("wallet", self.wallet),
                ("config", self.cfg),
            ]:
                self.store.db.execute(
                    "insert or replace into kv values(?,?)", (k, json.dumps(v))
                )
            self.store.db.commit()

    def events(self, kind, message, **kwargs):
        self.store.event(kind, dict(message=message, **kwargs))

    def balances(self):
        pnl = {v: 0.0 for v in self.wallet}
        used = {v: 0.0 for v in self.wallet}
        for p in self.positions:
            for leg in p["legs"]:
                if not leg.get("closed"):
                    used[leg["venue"]] += leg["margin"]
                    pnl[leg["venue"]] += leg.get("price_pnl", 0)
        return {
            v: dict(
                cash=self.wallet[v],
                equity=self.wallet[v] + pnl[v],
                margin=used[v],
                free=self.wallet[v] + pnl[v] - used[v],
            )
            for v in self.wallet
        }

    def daily_pnl(self):
        # Cash changes plus open liquidation estimate against the local Istanbul day-start baseline.
        day = (
            dt.datetime.fromtimestamp(now(), dt.timezone(dt.timedelta(hours=3)))
            .date()
            .isoformat()
        )
        equity = sum(self.wallet.values()) + sum(
            p.get("unrealized", 0) for p in self.positions
        )
        baseline = self.store.get("daily", {})
        if baseline.get("day") != day:
            baseline = {"day": day, "equity": equity}
            self.store.put("daily", baseline)
        return equity - baseline["equity"]

    def update_config(self, changes):
        limits = {
            "capital": (10, 1000000),
            "leverage": (1, 5),
            "reserve": (0.3, 0.9),
            "risk": (0.01, 1000),
            "daily_loss": (0.01, 10000),
            "size": (10, 100000),
            "horizon": (1, 72),
            "min_net": (0.01, 1000),
            "min_corr": (0.8, 1),
            "max_z": (0.5, 5),
            "poll_seconds": (15, 300),
            "latency_ms": (300, 5000),
            "extra_slippage_bps": (0, 50),
            "collateral_stress_bps": (0, 100),
            "extended_fee": (0.00025, 0.01),
            "lighter_fee": (0.00028, 0.01),
            "max_positions": (1, 3),
            "cooldown_seconds": (300, 86400),
        }
        if set(changes) - set(limits) - {"symbols"}:
            raise ValueError("Bilinmeyen ayar")
        fresh = dict(self.cfg)
        for k, v in changes.items():
            if k == "symbols":
                if (
                    not isinstance(v, list)
                    or len(v) != 15
                    or len(set(v)) != 15
                    or any(
                        not isinstance(a, str)
                        or not a.isalnum()
                        or not a.isupper()
                        or len(a) > 12
                        for a in v
                    )
                ):
                    raise ValueError("Tam 15 farklı büyük harfli varlık girin")
                if self.feed.meta and any(
                    a not in self.feed.meta.get("Lighter", {})
                    or a not in self.feed.meta.get("Extended", {})
                    for a in v
                ):
                    raise ValueError(
                        "Seçilen varlıklardan biri iki platformda ortak değil"
                    )
                fresh[k] = v
                continue
            n = number(v)
            lo, hi = limits[k]
            if not lo <= n <= hi:
                raise ValueError(k + " sınır dışında")
            fresh[k] = int(n) if isinstance(DEFAULTS[k], int) else n
        if fresh["daily_loss"] < fresh["risk"]:
            raise ValueError("Günlük kayıp limiti işlem bütçesinden küçük olamaz")
        if fresh["capital"] != self.cfg["capital"]:
            if self.positions or self.closed:
                raise ValueError(
                    "Sermaye işlem kayıtlarından sonra değiştirilemez; yeni veri klasörüyle ayrı test başlatın"
                )
            self.wallet = {
                "Lighter": fresh["capital"] / 2,
                "Extended": fresh["capital"] / 2,
            }
            self.store.put("daily", {})
        self.cfg = fresh
        self.persist()
        self.events("CONFIG", "Paper ayarları güncellendi")

    def evaluate(self, asset, lv, sv, books, analysis):
        cfg = self.cfg
        lm = self.feed.meta[lv][asset]
        sm = self.feed.meta[sv][asset]
        lb, sb = books[lv], books[sv]
        reasons = []
        for m in (lm, sm):
            if now() - m["received"] > 60 or m["rate"] is None:
                reasons.append("Piyasa veya funding verisi güncel değil")
        if (
            abs(lb["received"] - sb["received"]) > 5
            or now() - min(lb["received"], sb["received"]) > 30
        ):
            reasons.append("İki bacak defterleri güncel/senkron değil")
        leverage = min(cfg["leverage"], lm["max_leverage"], sm["max_leverage"])
        bal = self.balances()
        price = max(min(p for p, q in lb["asks"]), max(p for p, q in sb["bids"]))
        stress = analysis["stress_fraction"] + cfg["collateral_stress_bps"] / 10000
        fee = cfg["lighter_fee"] + cfg["extended_fee"]
        extra = cfg["extra_slippage_bps"] / 10000
        if self.daily_pnl() <= -cfg["daily_loss"]:
            raise ValueError("Günlük kayıp limiti doldu; yeni girişler engelli")
        budget = min(cfg["risk"], max(0, cfg["daily_loss"] + self.daily_pnl()))
        headroom = {v: self.entry_headroom(v) for v in (lv, sv)}

        # Per unit: margin plus this leg's entry fee at the worst fill open() accepts.
        def entry_need(v, px):
            return px * (1 + FILL_TOLERANCE) * (1 / leverage + self.fee(v))

        size = min(
            cfg["size"],
            *(headroom[v] / entry_need(v, 1) for v in (lv, sv)),
            budget / max(stress + 2 * fee + 4 * extra, 0.0001),
            sm.get("max_order", cfg["size"]),
        )
        step = max(Decimal(str(lm["step"])), Decimal(str(sm["step"])))
        qty = float(
            (Decimal(str(max(0, size) / price)) / step).to_integral_value(
                rounding=ROUND_DOWN
            )
            * step
        )
        # Size against actual quoted round-trip costs, not a fixed slippage assumption alone.
        for _ in range(6):
            if qty <= 0:
                break
            la = walk(lb["asks"], qty, True, extra)
            ss = walk(sb["bids"], qty, False, extra)
            lc = walk(lb["bids"], qty, False, extra)
            sc = walk(sb["asks"], qty, True, extra)
            quoted_loss = qty * (
                la * self.fee(lv)
                + ss * self.fee(sv)
                + lc * self.fee(lv)
                + sc * self.fee(sv)
                + (la - lc)
                + (sc - ss)
                + (la + ss) / 2 * stress
            )
            scale = min(
                budget / quoted_loss if quoted_loss > 0 else math.inf,
                headroom[lv] / (qty * entry_need(lv, la)),
                headroom[sv] / (qty * entry_need(sv, ss)),
            )
            if scale >= 1:
                break
            qty = float(
                (Decimal(str(qty * scale * 0.99)) / step).to_integral_value(
                    rounding=ROUND_DOWN
                )
                * step
            )
        if qty < max(lm["minimum"], sm["minimum"]) or qty * price < max(
            lm["min_quote"], sm["min_quote"], 10
        ):
            raise ValueError("Risk/marjin sınırı sonrası miktar minimum emrin altında")
        la = walk(lb["asks"], qty, True, extra)
        ss = walk(sb["bids"], qty, False, extra)
        lc = walk(lb["bids"], qty, False, extra)
        sc = walk(sb["asks"], qty, True, extra)
        notion = qty * (la + ss) / 2
        entry_fee = qty * (la * self.fee(lv) + ss * self.fee(sv))
        exit_fee = qty * (lc * self.fee(lv) + sc * self.fee(sv))
        execution_drag = qty * ((la - lc) + (sc - ss))
        # Use actual hourly payment schedules; no pro-rata credit for partial hours.
        end = now() + cfg["horizon"] * 3600
        counts = {v: max(0, int((end - hour(now())) // 3600)) for v in (lv, sv)}
        projected = qty * (
            -(lm["index"] if lv == "Lighter" else lm["mark"]) * lm["rate"] * counts[lv]
            + (sm["index"] if sv == "Lighter" else sm["mark"]) * sm["rate"] * counts[sv]
        )
        histl = self.feed.funding[(lv, asset)]
        hists = self.feed.funding[(sv, asset)]

        def mean24(rows):
            last = rows[-24:]
            if len(last) < 24 or any(
                abs((b["ts"] - a["ts"]) - 3600) > 10 for a, b in zip(last, last[1:])
            ):
                raise ValueError("24 saat funding geçmişi eksik")
            return statistics.mean(x["rate"] for x in last)

        historic = (
            qty
            * (
                (sm["index"] if sv == "Lighter" else sm["mark"]) * mean24(hists)
                - (lm["index"] if lv == "Lighter" else lm["mark"]) * mean24(histl)
            )
            * min(counts.values())
        )
        conservative = min(projected, historic)
        stress_loss = notion * stress
        net = conservative - entry_fee - exit_fee - execution_drag - stress_loss
        loss = entry_fee + exit_fee + execution_drag + stress_loss
        if min(analysis["corr24"], analysis["corr7"]) < cfg["min_corr"]:
            reasons.append("24s / 7g getiri korelasyonu sınır altında")
        if not 0.8 <= analysis["beta"] <= 1.2:
            reasons.append("Hedge beta uyumsuz")
        if abs(analysis["z"]) > cfg["max_z"]:
            reasons.append("Spread z-score sınırı aşıldı")
        if conservative <= 0:
            reasons.append("Kalıcı pozitif funding avantajı yok")
        if net < cfg["min_net"]:
            reasons.append("Funding geliri maliyet + spread stresini karşılamıyor")
        if loss > budget + 1e-6:
            reasons.append("İşlem kayıp bütçesi aşılıyor")
        if any(qty * entry_need(v, px) > headroom[v] for v, px in ((lv, la), (sv, ss))):
            reasons.append(
                "Marjin + giriş ücreti rezerv sonrası kullanılabilir marjini aşıyor"
            )
        if self.daily_pnl() <= -cfg["daily_loss"]:
            reasons.append("Günlük kayıp limiti doldu")
        if len(self.positions) >= cfg["max_positions"]:
            reasons.append("Pozisyon sayısı sınırı")
        if any(p["asset"] == asset for p in self.positions):
            reasons.append("Bu varlıkta açık pozisyon var")
        if any(
            p["asset"] == asset and now() - p["closed_at"] < cfg["cooldown_seconds"]
            for p in self.closed
        ):
            reasons.append("Kapanış sonrası bekleme süresi")
        return dict(
            asset=asset,
            long=lv,
            short=sv,
            qty=qty,
            size=notion,
            gross=qty * (la + ss),
            margin=qty * (la + ss) / leverage,
            leverage=leverage,
            projected=projected,
            conservative_funding=conservative,
            entry_fee=entry_fee,
            exit_fee=exit_fee,
            execution_drag=execution_drag,
            stress_loss=stress_loss,
            net=net,
            loss=loss,
            reasons=reasons,
            eligible=not reasons,
            analysis=analysis,
            entry_long=la,
            entry_short=ss,
            rate_long=lm["rate"],
            rate_short=sm["rate"],
            funding_counts=counts,
            audit_ids=[lb["audit_id"], sb["audit_id"], lm["audit_id"], sm["audit_id"]],
            funding_basis="Lighter: son gerçekleşen saatlik oran; Extended: güncel saatlik tahmin. Daha düşük 24s ortalama ile sınırlandı.",
        )

    def fee(self, v):
        return self.cfg["lighter_fee"] if v == "Lighter" else self.cfg["extended_fee"]

    def entry_headroom(self, v):
        """Margin plus entry fee a new leg may use on a venue while keeping the reserve."""
        bal = self.balances()[v]
        return max(0, bal["free"] - max(0, bal["equity"]) * self.cfg["reserve"])

    def quote(self, v, asset, qty, buy):
        b = self.feed.book(v, asset)
        return (
            walk(
                b["asks" if buy else "bids"],
                qty,
                buy,
                self.cfg["extra_slippage_bps"] / 10000,
            ),
            b,
        )

    def open(self, c):
        # Both legs are simulated sequentially. Crash recovery persists each leg immediately.
        p = dict(
            id=uuid.uuid4().hex,
            asset=c["asset"],
            opened=now(),
            legs=[],
            funding=0.0,
            estimated_funding=0.0,
            fees=0.0,
            unrealized=0.0,
            status="OPENING",
            decision=c,
            price_pnl=0.0,
            close_reason=None,
        )
        self.positions.append(p)
        self.persist()
        try:
            for venue, side in ((c["long"], "long"), (c["short"], "short")):
                time.sleep(self.cfg["latency_ms"] / 1000)
                px, b = self.quote(venue, c["asset"], c["qty"], side == "long")
                planned = c["entry_long"] if side == "long" else c["entry_short"]
                if abs(px / planned - 1) > FILL_TOLERANCE:
                    raise ValueError("Dolum öncesi fiyat %0,1 sınırını aştı")
                fee = c["qty"] * px * self.fee(venue)
                margin = c["qty"] * px / c["leverage"]
                if margin + fee > self.entry_headroom(venue):
                    raise ValueError("Dolum öncesi marjin rezervi yetersiz")
                leg = dict(
                    venue=venue,
                    side=side,
                    qty=c["qty"],
                    entry=px,
                    entered=now(),
                    fee_rate=self.fee(venue),
                    entry_fee=fee,
                    margin=margin,
                    closed=False,
                    price_pnl=0.0,
                    audit_id=b["audit_id"],
                )
                p["legs"].append(leg)
                p["fees"] += fee
                self.wallet[venue] -= fee
                self.persist()
                self.events(
                    "PAPER_FILL", "Sanal bacak doldu", position=p["id"], leg=leg
                )
            p["status"] = "OPEN"
            self.persist()
            self.events(
                "PAPER_OPEN",
                "Risk kontrolleri sonrası iki bacak açıldı",
                position=p["id"],
                decision=c,
            )
        except Exception as ex:
            p["status"] = "RECOVERY"
            p["close_reason"] = "Bacak kurtarma: " + str(ex)
            self.persist()
            self.events("RECOVERY", p["close_reason"], position=p["id"])
            self.close(p, p["close_reason"])

    def funding_valuation(self, leg, asset, payment_time):
        """Recover a missing mark without silently discarding cash or using future prices."""
        observed = self.store.before(leg["venue"], asset, payment_time)
        if observed:
            return dict(
                price=observed[1], ts=observed[0], method="observed_mark", audit_id=None
            )
        failure = "Geçmiş fiyat bağlantısı bulunmuyor"
        try:
            reference = self.feed.funding_reference(asset, payment_time)
            reference["price"] = number(reference["price"])
            reference["ts"] = number(reference["ts"])
            if (
                not 0 < reference["price"]
                or not payment_time - 3600 <= reference["ts"] <= payment_time
            ):
                raise ValueError("Geçmiş değerleme zamanı/fiyatı geçersiz")
            return reference
        except Exception as ex:
            failure = str(ex)
        # Bounded waiting for public history. The actual recorded entry fill is a
        # reproducible estimate, never represented as a settlement mark or zero cash.
        if (
            now() - payment_time >= 3600
            and leg["entered"] < payment_time
            and number(leg["entry"]) > 0
        ):
            return dict(
                price=leg["entry"],
                ts=leg["entered"],
                method="entry_fill_fallback",
                audit_id=leg.get("audit_id"),
                warning=failure,
            )
        return None

    def settle(self, p):
        pending = []
        for leg in p["legs"]:
            venue = leg["venue"]
            rows = self.feed.funding.get((venue, p["asset"]), [])
            expected_end = min(now(), leg.get("exited", now()))
            first = hour(leg["entered"]) + 3600
            expected = set(range(first, hour(expected_end) + 1, 3600))
            if leg.get("exited") and leg["exited"] == hour(leg["exited"]):
                expected.discard(hour(leg["exited"]))
            with self.store.lock:
                recorded = self.store.db.execute(
                    "select ts from payments where key like ?",
                    (p["id"] + ":" + venue + ":%",),
                ).fetchall()
            seen = {hour(record[0]) for record in recorded}
            for r in rows:
                if not leg["entered"] < r["ts"] < expected_end:
                    continue
                seen.add(hour(r["ts"]))
                key = p["id"] + ":" + venue + ":" + str(hour(r["ts"]))
                with self.store.lock:
                    exists = self.store.db.execute(
                        "select body from payments where key=?", (key,)
                    ).fetchone()
                late = bool(exists) and json.loads(exists[0]).get("unverified", False)
                if exists and not late:
                    continue
                estimated = False
                valuation = None
                if venue == "Lighter":
                    unit_value = r["value"]
                else:
                    valuation = self.funding_valuation(leg, p["asset"], r["ts"])
                    if not valuation:
                        pending.append(key)
                        continue
                    unit_value = valuation["price"] * r["rate"]
                    estimated = True
                value = leg["qty"] * unit_value * (-1 if leg["side"] == "long" else 1)
                body = dict(
                    position=p["id"],
                    venue=venue,
                    asset=p["asset"],
                    rate=r["rate"],
                    cash=value,
                    valuation_estimated=estimated,
                    audit_id=r["audit_id"],
                    valuation=valuation,
                )
                # Atomic payment, wallet, and position persistence, so restart cannot credit twice.
                with self.store.lock:
                    self.wallet[venue] += value
                    p["funding"] += value
                    if estimated:
                        p["estimated_funding"] += value
                    if valuation:
                        p.setdefault("funding_valuations", []).append(
                            dict(
                                payment_time=r["ts"],
                                venue=venue,
                                cash=value,
                                **valuation,
                            )
                        )
                    if late:
                        p["funding_unverified"] = [
                            x
                            for x in p.get("funding_unverified", [])
                            if (x["venue"], x["payment_time"]) != (venue, hour(r["ts"]))
                        ]
                    self.store.db.execute(
                        "insert or replace into payments values(?,?,?)",
                        (key, r["ts"], json.dumps(body)),
                    )
                    self.persist()
                if late:
                    self.events(
                        "FUNDING_LATE",
                        "Doğrulanmamış funding saati geç yayımlanan oranla uzlaştırıldı",
                        position=p["id"],
                        venue=venue,
                        payment_time=r["ts"],
                        cash=value,
                    )
                if valuation and valuation["method"] != "observed_mark":
                    self.events(
                        "FUNDING_ESTIMATE",
                        "Eksik ödeme markı tahmini geçmiş değerlemeyle uzlaştırıldı",
                        position=p["id"],
                        payment_time=r["ts"],
                        valuation=valuation,
                    )
            missing = expected - seen
            # No rate is invented: after a bounded wait the hour is kept as unverified
            # zero cash, visible in the record, instead of blocking entries forever.
            for t in sorted(
                x for x in missing if now() - x >= FUNDING_UNVERIFIED_AFTER
            ):
                key = p["id"] + ":" + venue + ":" + str(t)
                body = dict(
                    position=p["id"],
                    venue=venue,
                    asset=p["asset"],
                    rate=None,
                    cash=0.0,
                    valuation_estimated=False,
                    audit_id=None,
                    valuation=None,
                    unverified=True,
                )
                with self.store.lock:
                    p.setdefault("funding_unverified", []).append(
                        dict(payment_time=t, venue=venue, recorded=now())
                    )
                    self.store.db.execute(
                        "insert into payments values(?,?,?)",
                        (key, t, json.dumps(body)),
                    )
                    self.persist()
                missing.discard(t)
                self.events(
                    "FUNDING_UNVERIFIED",
                    "Funding oranı yayımlanmadı; saat doğrulanmamış 0 tutarla kaydedildi, girişler açıldı",
                    position=p["id"],
                    venue=venue,
                    payment_time=t,
                )
            if missing:
                pending.append(venue + ": funding saatleri eksik")
        p["pending_funding"] = pending

    def mark_position(self, p, books):
        unrealized = 0.0
        exit_fee = 0.0
        legs_open = 0
        for leg in p["legs"]:
            if leg["closed"]:
                continue
            b = books.get(leg["venue"])
            if not b:
                raise ValueError("Açık bacak fiyatı yok; son değer korunuyor")
            buy = leg["side"] == "short"
            px = walk(
                b["asks" if buy else "bids"],
                leg["qty"],
                buy,
                self.cfg["extra_slippage_bps"] / 10000,
            )
            leg["price_pnl"] = (
                leg["qty"] * (px - leg["entry"]) * (1 if leg["side"] == "long" else -1)
            )
            leg["exit_estimate"] = px
            unrealized += leg["price_pnl"]
            exit_fee += leg["qty"] * px * leg["fee_rate"]
            legs_open += 1
        p["unrealized"] = unrealized - exit_fee
        p["price_pnl_open"] = unrealized
        p["net_if_closed"] = p["price_pnl"] + p["funding"] - p["fees"] + p["unrealized"]
        p["last_mark"] = now()
        return legs_open

    def close(self, p, reason):
        p["status"] = "CLOSING"
        p["close_reason"] = reason
        self.persist()
        for leg in p["legs"]:
            if leg["closed"]:
                continue
            try:
                time.sleep(self.cfg["latency_ms"] / 1000)
                px, b = self.quote(
                    leg["venue"], p["asset"], leg["qty"], leg["side"] == "short"
                )
                pnl = (
                    leg["qty"]
                    * (px - leg["entry"])
                    * (1 if leg["side"] == "long" else -1)
                )
                fee = leg["qty"] * px * leg["fee_rate"]
                self.wallet[leg["venue"]] += pnl - fee
                p["price_pnl"] += pnl
                p["fees"] += fee
                leg.update(
                    closed=True,
                    exit=px,
                    exited=now(),
                    price_pnl=0.0,
                    exit_audit_id=b["audit_id"],
                )
                p["unrealized"] = sum(
                    x.get("price_pnl", 0)
                    - x["qty"] * x.get("exit_estimate", x["entry"]) * x["fee_rate"]
                    for x in p["legs"]
                    if not x["closed"]
                )
                self.persist()
            except Exception as ex:
                p["status"] = "RECOVERY"
                self.events(
                    "RECOVERY",
                    "Kapatılamayan sanal bacak; yeni girişler kapalı",
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
        self.events("PAPER_CLOSE", reason, position=p["id"], net=p["net"])

    def cycle(self):
        if self.busy:
            return
        self.busy = True
        try:
            self.error = None
            self.feed.markets()
            meta = self.feed.meta
            if not all(v in meta for v in ("Lighter", "Extended")):
                raise ValueError(
                    "İki platform birlikte erişilebilir değil; yeni girişler durdu"
                )
            assets = [
                a
                for a in self.cfg["symbols"]
                if all(a in meta[v] for v in ("Lighter", "Extended"))
            ]
            missing = [a for a in self.cfg["symbols"] if a not in assets]
            # RFQ markets have no executable public book; label them without fetching one.
            rfq = [
                a
                for a in assets
                if any(meta[v][a].get("rfq") for v in ("Lighter", "Extended"))
            ]
            assets = [a for a in assets if a not in rfq]
            assets = list(dict.fromkeys([p["asset"] for p in self.positions] + assets))
            books = {}
            self.candidates = []
            for a in rfq:
                for lv, sv in (("Lighter", "Extended"), ("Extended", "Lighter")):
                    self.candidates.append(
                        dict(
                            asset=a,
                            long=lv,
                            short=sv,
                            eligible=False,
                            reasons=["RFQ yürütmesi henüz desteklenmiyor"],
                            net=None,
                        )
                    )
            for a in missing:
                for lv, sv in (("Lighter", "Extended"), ("Extended", "Lighter")):
                    self.candidates.append(
                        dict(
                            asset=a,
                            long=lv,
                            short=sv,
                            eligible=False,
                            reasons=[
                                "İki platformda ortak ve aktif perpetual piyasa yok"
                            ],
                            net=None,
                        )
                    )

            def prep(a):
                bs = None
                hs = None
                fs = None
                try:
                    earliest = min(
                        [
                            leg["entered"]
                            for p in self.positions + self.closed
                            if p["asset"] == a
                            and (p in self.positions or p.get("pending_funding"))
                            for leg in p["legs"]
                        ]
                        + [now() - 7 * 86400]
                    )
                    fs = {
                        v: self.feed.funding_history(v, a, earliest)
                        for v in ("Lighter", "Extended")
                    }
                    bs = {v: self.feed.book(v, a) for v in ("Lighter", "Extended")}
                    hs = {v: self.feed.candles(v, a) for v in ("Lighter", "Extended")}
                    return a, hs, fs, bs, None
                except Exception as ex:
                    # History/rate failure must never suppress live risk observation on existing legs.
                    if any(p["asset"] == a for p in self.positions):
                        try:
                            bs = {
                                v: self.feed.book(v, a) for v in ("Lighter", "Extended")
                            }
                        except Exception:
                            pass
                    return a, hs, fs, bs, str(ex)

            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                prepared = list(pool.map(prep, assets))
            for a, hs, fs, bs, error in prepared:
                if bs:
                    books[a] = bs
                if error:
                    for lv, sv in (("Lighter", "Extended"), ("Extended", "Lighter")):
                        self.candidates.append(
                            dict(
                                asset=a,
                                long=lv,
                                short=sv,
                                eligible=False,
                                reasons=[error],
                                net=None,
                            )
                        )
                    self.events("REJECT", error, asset=a)
                    continue
                books[a] = bs
                for lv, sv in (("Lighter", "Extended"), ("Extended", "Lighter")):
                    try:
                        analysis = analyze(hs[lv], hs[sv], self.cfg["horizon"], now())
                        c = self.evaluate(a, lv, sv, bs, analysis)
                    except Exception as ex:
                        c = dict(
                            asset=a,
                            long=lv,
                            short=sv,
                            eligible=False,
                            reasons=[str(ex)],
                            net=None,
                        )
                    self.candidates.append(c)
                    self.store.event("DECISION", c)
            # Reconcile settlement before evaluating exits; pending payments block future entries.
            for p in list(self.positions):
                self.settle(p)
                if p["asset"] not in books:
                    p["status"] = "DATA_WAIT" if p["status"] == "OPEN" else p["status"]
                    self.events(
                        "DATA_WAIT",
                        "Fiyat güncel değil: risk ölçülemiyor, açık pozisyon korunuyor",
                        position=p["id"],
                    )
                    continue
                try:
                    if (
                        now() - min(b["received"] for b in books[p["asset"]].values())
                        > 30
                    ):
                        books[p["asset"]] = {
                            v: self.feed.book(v, p["asset"])
                            for v in ("Lighter", "Extended")
                        }
                    self.mark_position(p, books[p["asset"]])
                except Exception as ex:
                    self.events("DATA_WAIT", str(ex), position=p["id"])
                    continue
                reason = None
                if p["status"] in ("RECOVERY", "CLOSING", "OPENING"):
                    reason = p.get("close_reason") or "Yarım kalan işlem kurtarma"
                elif p["id"] in self.manual_close:
                    reason = "Kullanıcı paper kapatma talebi"
                elif p["net_if_closed"] <= -self.cfg["risk"]:
                    reason = "Pozisyon kayıp limiti"
                elif self.daily_pnl() <= -self.cfg["daily_loss"]:
                    reason = "Günlük kayıp limiti"
                elif (
                    p["price_pnl_open"] + p["decision"]["execution_drag"]
                    < -p["decision"]["stress_loss"]
                ):
                    reason = "Spread stres bütçesi aşıldı"
                elif any(
                    self.balances()[leg["venue"]]["equity"]
                    < leg["qty"]
                    * meta[leg["venue"]][p["asset"]]["mark"]
                    * meta[leg["venue"]][p["asset"]]["mm"]
                    * 2
                    for leg in p["legs"]
                    if not leg["closed"]
                ):
                    reason = "Bacak bazında muhafazakâr marjin alarmı"
                else:
                    pair = next(
                        (
                            c
                            for c in self.candidates
                            if c.get("asset") == p["asset"]
                            and c.get("long") == p["decision"]["long"]
                        ),
                        None,
                    )
                    if pair and "analysis" in pair:
                        if (
                            min(pair["analysis"]["corr24"], pair["analysis"]["corr7"])
                            < self.cfg["min_corr"]
                            or not 0.8 <= pair["analysis"]["beta"] <= 1.2
                        ):
                            reason = "Hedge uyumu bozuldu"
                        elif abs(pair["analysis"]["z"]) > self.cfg["max_z"]:
                            reason = "Spread z-score limiti"
                        # Remaining advantage excludes already-paid entry costs; future hourly funding versus exit fee and stress.
                    lm = meta[p["decision"]["long"]][p["asset"]]
                    sm = meta[p["decision"]["short"]][p["asset"]]
                    if lm["rate"] is None or sm["rate"] is None:
                        reason = reason or "Funding verisi kayboldu"
                        remaining = -1e9
                    else:
                        remaining = (
                            p["decision"]["qty"]
                            * (
                                (
                                    sm["index"]
                                    if p["decision"]["short"] == "Lighter"
                                    else sm["mark"]
                                )
                                * sm["rate"]
                                - (
                                    lm["index"]
                                    if p["decision"]["long"] == "Lighter"
                                    else lm["mark"]
                                )
                                * lm["rate"]
                            )
                            * self.cfg["horizon"]
                        )
                    remaining -= (
                        sum(
                            leg["qty"]
                            * leg.get("exit_estimate", leg["entry"])
                            * leg["fee_rate"]
                            for leg in p["legs"]
                            if not leg["closed"]
                        )
                        + p["decision"]["stress_loss"]
                    )
                    p["remaining_net_estimate"] = remaining
                    if remaining < self.cfg["min_net"]:
                        reason = (
                            reason
                            or "Kalan funding avantajı çıkış maliyeti ve stresi karşılamıyor"
                        )
                if reason:
                    self.close(p, reason)
                else:
                    p["status"] = "OPEN"
            # Late published funding can be reconciled on closed paper trades without fabricating valuation.
            for p in self.closed:
                if p.get("pending_funding"):
                    self.settle(p)
                    p["net"] = p["price_pnl"] + p["funding"] - p["fees"]
            all_pending = any(
                p.get("pending_funding") for p in self.positions + self.closed
            )
            candidates = sorted(
                [c for c in self.candidates if c.get("eligible")],
                key=lambda c: c["net"],
                reverse=True,
            )
            if (
                self.running
                and not all_pending
                and not any(p["status"] != "OPEN" for p in self.positions)
                and candidates
                and len(self.positions) < self.cfg["max_positions"]
            ):
                best = candidates[0]
                # Long-running collection can stale earlier books: re-read and fully re-evaluate before entry.
                bs = {
                    v: self.feed.book(v, best["asset"]) for v in ("Lighter", "Extended")
                }
                fresh = self.evaluate(
                    best["asset"], best["long"], best["short"], bs, best["analysis"]
                )
                if fresh["eligible"] and self.running:
                    self.open(fresh)
            self.candidates.sort(
                key=lambda c: (not c["eligible"], -(c.get("net") or -1e9))
            )
            self.last_cycle = now()
            self.persist()
        except Exception as ex:
            self.error = str(ex)
            self.events("ERROR", str(ex))
            self.persist()
        finally:
            self.busy = False

    def loop(self):
        while not self.stop_event.is_set():
            with self.lock:
                self.cycle()
                self.public_snapshot = json.loads(json.dumps(self.snapshot()))
            self.stop_event.wait(self.cfg["poll_seconds"])

    def snapshot(self):
        return dict(
            mode="PAPER",
            running=self.running,
            busy=self.busy,
            config=self.cfg,
            connections=self.connections.view(),
            balances=self.balances(),
            daily_pnl=self.daily_pnl(),
            positions=self.positions,
            closed=self.closed[-30:],
            candidates=self.candidates[:30],
            platforms=self.feed.status,
            last_cycle=self.last_cycle,
            error=self.error,
            events=self.store.recent("events", 60),
            payments=self.store.recent("payments", 60),
            pending_funding=any(
                p.get("pending_funding") for p in self.positions + self.closed
            ),
            valuation="Paper dolumlar: sıralı REST emir defteri VWAP + gecikme + ek slippage. Ödeme: gerçekleşen oran, Lighter birim ödeme; Extended gözlenen son mark ile tahmini tutar. USD/USDC=1 varsayımı + teminat stres payı.",
        )


class ExclusiveServer(ThreadingHTTPServer):
    """Refuse a port another server already holds.

    HTTPServer sets SO_REUSEADDR, which on Windows lets a second process bind the same
    port while the first keeps answering: the browser then silently shows the old panel.
    """

    allow_reuse_address = False

    def server_bind(self):
        import socket

        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        # A browser that reloads or closes the page mid-response is not an engine error.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


# A panel action waits for the running scan to finish instead of being refused; a scan
# across every venue can take minutes.
ACTION_LOCK_WAIT = 240


def serve(engine, port, host="127.0.0.1", password=None):
    """Local by default. A non-loopback bind (hosted deploy) requires a dashboard password."""
    token = uuid.uuid4().hex
    local = host in ("127.0.0.1", "localhost")
    if not local and not password:
        raise SystemExit("Uzak erişim için NB_DASHBOARD_PASSWORD ayarlanmalı")
    expected = (
        "Basic " + base64.b64encode(f"paper:{password}".encode()).decode()
        if password
        else None
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def allowed(self):
            # Local mode guards against DNS rebinding by Host; hosted mode by HTTP Basic auth.
            if expected:
                return hmac.compare_digest(
                    self.headers.get("Authorization", ""), expected
                )
            return self.headers.get("Host") in (
                f"127.0.0.1:{port}",
                f"localhost:{port}",
            )

        def challenge(self):
            raw = json.dumps({"error": "Panel şifresi gerekli"}).encode()
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="neutralbasis"')
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(raw)

        def send(self, obj, status=200):
            raw = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/healthz":
                return self.send({"ok": True, "mode": "PAPER"})
            if not self.allowed():
                if expected:
                    return self.challenge()
                return self.send({"error": "Yerel host gerekli"}, 403)
            if self.path == "/api/state":
                # A cycle performs HTTP I/O; read state only between atomic updates to avoid torn JSON.
                if not engine.lock.acquire(timeout=0.1):
                    return self.send(
                        {
                            **engine.public_snapshot,
                            "busy": True,
                            "running": engine.running,
                            "connections": engine.connections.view(),
                        }
                        if engine.public_snapshot
                        else {"busy": True}
                    )
                try:
                    return self.send(engine.snapshot())
                finally:
                    engine.lock.release()
            if self.path == "/api/audit-export":
                with engine.store.lock:
                    rows = engine.store.db.execute(
                        "select id,ts,url,sha from audit order by id desc limit 500"
                    ).fetchall()
                return self.send(
                    {
                        "audit": rows,
                        "events": engine.store.recent("events", 1000),
                        "payments": engine.store.recent("payments", 1000),
                        "closed": engine.closed,
                        "positions": engine.positions,
                    }
                )
            if self.path == "/api/connections":
                return self.send(engine.connections.view())
            if self.path == "/api/backtest" and hasattr(engine, "backtest_status"):
                return self.send(engine.backtest_status())
            if self.path == "/api/funding-matrix" and hasattr(engine, "funding_matrix"):
                return self.send(engine.funding_matrix())
            if self.path.startswith(("/api/asset?", "/api/pair?")) and hasattr(
                engine, "pair_view"
            ):
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                arg = lambda k: q.get(k, [""])[0]
                try:
                    if self.path.startswith("/api/asset?"):
                        return self.send(engine.asset_view(arg("asset")))
                    return self.send(
                        engine.pair_view(arg("asset"), arg("long"), arg("short"))
                    )
                except ValueError as ex:
                    return self.send({"error": str(ex)}, 404)
            if self.path == "/api/read-jobs" and hasattr(engine, "read_status"):
                return self.send(engine.read_status())
            if self.path == "/api/report.csv":
                if not engine.lock.acquire(timeout=0.1):
                    return self.send(
                        {"error": "Tarama sürüyor; tamamlanınca tekrar deneyin"}, 409
                    )
                try:
                    from desk_analytics import report_csv

                    raw = ("\ufeff" + report_csv(engine)).encode("utf8")
                finally:
                    engine.lock.release()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header(
                    "Content-Disposition",
                    'attachment; filename="neutralbasis-paper.csv"',
                )
                self.end_headers()
                return self.wfile.write(raw)
            if self.path == "/api/wide-trades.csv" and hasattr(engine, "wide_paper"):
                from wide_paper import trades_csv

                trades = engine.wide_paper.trade_log()
                raw = ("\ufeff" + trades_csv(trades)).encode("utf8")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header(
                    "Content-Disposition",
                    'attachment; filename="neutralbasis-coklu-paper.csv"',
                )
                self.end_headers()
                return self.wfile.write(raw)
            if self.path == "/api/identity":
                return self.send(
                    {"pid": os.getpid(), "root": str(ROOT), "mode": "PAPER"}
                )
            if self.path.split("?")[0] in (
                "/",
                "/scanner",
                "/positions",
                "/settings",
                "/backtest",
            ):
                raw = (
                    (ROOT / "index.html")
                    .read_text(encoding="utf8")
                    .replace("__TOKEN__", token)
                    .encode()
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(raw)
            self.send({"error": "Bulunamadı"}, 404)

        def do_POST(self):
            if not self.allowed() or self.headers.get("X-Paper-Token") != token:
                return self.send({"error": "Yerel panel doğrulaması gerekli"}, 403)
            length = int(self.headers.get("Content-Length", "0"))
            if length > 10000:
                return self.send({"error": "İstek çok büyük"}, 400)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/connections/test":
                    return self.send(engine.connections.test(body.get("venue")))
                if self.path == "/api/connections/extended-key":
                    return self.send(
                        engine.connections.save_extended_key(body.get("key"))
                    )
                if self.path == "/api/connections/delete-key":
                    return self.send(engine.connections.delete_extended_key())
                if self.path == "/api/connections/retest-key":
                    return self.send(engine.connections.retest_key())
                if self.path in ("/api/history", "/api/quote") and hasattr(
                    engine, "request_read"
                ):
                    return self.send(
                        engine.request_read(self.path.split("/")[-1], body)
                    )
                if self.path == "/api/pair-book" and hasattr(engine, "pair_books"):
                    return self.send(engine.pair_books(body))
                if self.path == "/api/pair-sync" and hasattr(
                    engine, "request_pair_sync"
                ):
                    return self.send(engine.request_pair_sync(body))
                if self.path == "/api/shutdown":
                    engine.running = False
                    engine.stop_event.set()
                    self.send(
                        {"ok": True, "message": "Motor kapanıyor; yeni girişler durdu."}
                    )
                    threading.Thread(target=httpd.shutdown, daemon=True).start()
                    return
                # Pause remains responsive while a scan is waiting on an external data provider.
                if self.path in ("/api/start", "/api/pause"):
                    engine.running = self.path == "/api/start"
                    engine.events(
                        "START" if engine.running else "PAUSE",
                        (
                            "Otomatik paper girişler etkin"
                            if engine.running
                            else "Yeni girişler durdu; açık pozisyonlar izleniyor"
                        ),
                    )
                    return self.send({"ok": True})
                if not engine.lock.acquire(timeout=ACTION_LOCK_WAIT):
                    return self.send(
                        {
                            "error": "Tarama 4 dakikadan uzun sürüyor; işlem uygulanmadı, birazdan tekrar deneyin"
                        },
                        409,
                    )
                try:
                    if self.path == "/api/start":
                        engine.running = True
                        engine.events("START", "Otomatik paper girişler etkin")
                    elif self.path == "/api/pause":
                        engine.running = False
                        engine.events(
                            "PAUSE", "Yeni girişler durdu; açık pozisyonlar izleniyor"
                        )
                    elif self.path == "/api/config":
                        engine.update_config(body)
                    elif self.path == "/api/wide_reset" and hasattr(
                        engine, "wide_paper"
                    ):
                        engine.wide_paper.reset(
                            body.get("capital"),
                            body.get("venue_capital"),
                            body.get("venues"),
                        )
                    elif self.path == "/api/wide_fees" and hasattr(
                        engine, "wide_paper"
                    ):
                        engine.wide_paper.set_fees(body.get("fees") or {})
                    elif self.path == "/api/backtest" and hasattr(
                        engine, "request_backtest"
                    ):
                        return self.send(engine.request_backtest(body))
                    elif self.path == "/api/close":
                        wide = getattr(engine, "wide_paper", None)
                        if wide and any(
                            p["id"] == body.get("id") for p in wide.positions
                        ):
                            wide.manual_close.add(body["id"])
                        elif not any(
                            p["id"] == body.get("id") for p in engine.positions
                        ):
                            raise ValueError("Pozisyon bulunamadı")
                        else:
                            engine.manual_close.add(body["id"])
                        engine.events(
                            "CLOSE_REQUEST",
                            "Sonraki taramada paper kapatma istendi",
                            position=body["id"],
                        )
                    else:
                        raise ValueError("Bilinmeyen işlem")
                    self.send({"ok": True})
                finally:
                    engine.lock.release()
            except (ValueError, TypeError, KeyError) as ex:
                self.send({"error": str(ex)}, 400)

    try:
        httpd = ExclusiveServer((host, port), Handler)
    except OSError as ex:
        raise SystemExit(
            f"{host}:{port} portu başka bir işlem tarafından kullanılıyor ({ex}). "
            "Eski motoru kapatın (Durdur.cmd veya Görev Yöneticisi'nde python) ya da --port ile başka port seçin."
        )
    httpd.daemon_threads = True
    engine.public_snapshot = json.loads(json.dumps(engine.snapshot()))
    print(f"Funding Desk Paper: http://{host}:{port}", flush=True)

    # Hosted platforms stop the process with SIGTERM; take the same path as Ctrl+C so state persists.
    def stop(*_):
        raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, stop)
    worker = threading.Thread(target=engine.loop, daemon=True)
    worker.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop_event.set()
        worker.join()
        httpd.server_close()
        engine.persist()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", default=str(ROOT / "data"))
    parser.add_argument("--autostart", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    path = Path(args.data)
    path.mkdir(parents=True, exist_ok=True)
    engine = Engine(path / "paper.sqlite3")
    engine.running = args.autostart
    if args.once:
        engine.cycle()
        print(json.dumps(engine.snapshot(), ensure_ascii=False, allow_nan=False))
    else:
        serve(engine, args.port)
